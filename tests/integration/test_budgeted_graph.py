"""验证真实图路径的预算前置、耗尽停止及恢复计数。"""

from datetime import timedelta

import pytest
from sqlalchemy import update

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.processor import LangGraphMultiAgentProcessor
from opsswarm.worker.runner import InvestigationWorker
from tests.checkpoint_fakes import (
    ConclusionSynthesizerStub,
    CountingDataSource,
    SpecialistAnalystStub,
)
from tests.integration.test_fixed_task_graph import FailLogsAfterCommittedPeers, factory
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class CountingAnalyst(SpecialistAnalystStub):
    def __init__(self):
        super().__init__()
        self.calls = 0

    async def analyze(self, **kwargs):
        self.calls += 1
        return await super().analyze(**kwargs)


async def run_worker(db, url, store, clock, source, analyst):
    async with open_checkpoint_saver(url) as saver:
        processor = LangGraphMultiAgentProcessor(
            investigator=LangGraphParallelInvestigator(
                data_source=source,
                analyst=analyst,
                synthesizer=ConclusionSynthesizerStub(),
                checkpointer=saver,
            ),
            task_investigator_factory=factory(db, saver, source, clock, analyst),
            model_name="fake-budget-model",
        )
        worker = InvestigationWorker(
            store=store,
            processor=processor,
            worker_id="budget-worker",
            clock=clock,
            base_retry_delay=timedelta(0),
            max_retry_delay=timedelta(0),
        )
        return await worker.run_once()


@pytest.mark.parametrize(
    "policy,expected_models,expected_tools",
    [
        (CallBudgetPolicy(), 4, 3),
        (CallBudgetPolicy(max_model_calls=1), 1, None),
        (CallBudgetPolicy(max_model_calls=3), 3, 3),
        (CallBudgetPolicy(max_tool_calls=1), None, 1),
    ],
)
async def test_graph_reserves_before_calls_and_stops_without_retry(
    database_session_factory, checkpoint_database_url, policy, expected_models, expected_tools
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, started = await started_run(
        database_session_factory, workflow_version=WorkflowVersion.V2, call_budget=policy
    )
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    source = CountingDataSource()
    analyst = CountingAnalyst()
    assert await run_worker(
        database_session_factory, checkpoint_database_url, parent, clock, source, analyst
    )
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(started.run.id)
    run = await parent.get_run(started.run.id)
    if expected_models is not None:
        assert budget.reserved_model_calls == expected_models
    if expected_tools is not None:
        assert budget.reserved_tool_calls == expected_tools
    assert analyst.calls <= budget.reserved_model_calls
    assert sum(source.calls.values()) <= budget.reserved_tool_calls
    assert all(call.status != "reserved" for call in budget.calls)
    assert [call.sequence for call in budget.calls] == list(range(1, len(budget.calls) + 1))
    if policy == CallBudgetPolicy():
        assert run.status == "succeeded"
        assert len(budget.calls) == 7 and all(call.status == "succeeded" for call in budget.calls)
        assert analyst.calls == 3 and sum(source.calls.values()) == 3
        assert sum(call.operation == "synthesize" for call in budget.calls) == 1
    else:
        assert run.status == "failed" and "budget_exceeded" in run.last_error.error_code
        assert (await parent.get_work_item(started.work_item.id)).attempt_count == 1
        assert not await run_worker(
            database_session_factory, checkpoint_database_url, parent, clock, source, analyst
        )
        tasks = (
            (await CollaborationQueries(database_session_factory).get_snapshot(run.id))
            .rounds[0]
            .tasks
        )
        assert all(task.status in ("succeeded", "failed") for task in tasks)


@pytest.mark.parametrize("legacy", [False, True])
async def test_retry_reuses_successful_tasks_but_accumulates_model_and_tool_slots(
    database_session_factory, checkpoint_database_url, legacy
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, started = await started_run(
        database_session_factory, workflow_version=WorkflowVersion.V2
    )
    if legacy:
        # 模拟迁移前的运行；新策略不能隐式改变其恢复语义。
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == started.run.id)
                .values(evidence_policy_version=None)
            )
    assert (await parent.get_run(started.run.id)).evidence_policy_version == (
        None if legacy else "task-evidence-v1"
    )
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    source = CountingDataSource()
    queries = CollaborationQueries(database_session_factory)
    await run_worker(
        database_session_factory,
        checkpoint_database_url,
        parent,
        clock,
        source,
        FailLogsAfterCommittedPeers(queries, started.run.id),
    )
    before = await CallBudgetQueries(database_session_factory).get_snapshot(started.run.id)
    assert before.reserved_model_calls == 3 and before.reserved_tool_calls == 3
    assert (await parent.get_run(started.run.id)).status == "queued"
    clock.current += timedelta(seconds=1)
    recovered = CountingDataSource()
    await run_worker(
        database_session_factory,
        checkpoint_database_url,
        parent,
        clock,
        recovered,
        SpecialistAnalystStub(),
    )
    after = await CallBudgetQueries(database_session_factory).get_snapshot(started.run.id)
    assert after.reserved_model_calls == 5
    assert after.reserved_tool_calls == (4 if legacy else 3)
    assert sum(recovered.calls.values()) == (1 if legacy else 0)
    assert after.started_at == before.started_at and after.deadline == before.deadline
    assert (await parent.get_run(started.run.id)).status == "succeeded"


async def test_retry_after_deadline_stops_before_new_external_calls(
    database_session_factory, checkpoint_database_url
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        call_budget=CallBudgetPolicy(max_duration_seconds=3),
    )
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    await run_worker(
        database_session_factory,
        checkpoint_database_url,
        parent,
        clock,
        CountingDataSource(),
        FailLogsAfterCommittedPeers(CollaborationQueries(database_session_factory), started.run.id),
    )
    before = await CallBudgetQueries(database_session_factory).get_snapshot(started.run.id)
    clock.current += timedelta(seconds=3)
    source, analyst = CountingDataSource(), CountingAnalyst()
    await run_worker(
        database_session_factory, checkpoint_database_url, parent, clock, source, analyst
    )
    after = await CallBudgetQueries(database_session_factory).get_snapshot(started.run.id)
    run = await parent.get_run(started.run.id)
    assert run.status == "failed" and run.last_error.error_code == "run_deadline_exceeded"
    assert after == before
    assert analyst.calls == 0 and sum(source.calls.values()) == 0
    assert not await run_worker(
        database_session_factory, checkpoint_database_url, parent, clock, source, analyst
    )
