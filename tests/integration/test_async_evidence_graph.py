"""有界线程查询与持久预算、任务结果和租约共同生效。"""

import asyncio
from datetime import timedelta

import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallOperation
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.models import EvidenceKind, WorkItemError
from opsswarm.investigation.query_execution import (
    EvidenceQueryCapacityExceeded,
    EvidenceQueryTimeout,
    ReadOnlyQueryExecutor,
)
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.task_graph import TaskGraphFactory
from tests.async_evidence_helpers import BlockedQuery, drain
from tests.checkpoint_fakes import (
    ConclusionSynthesizerStub,
    CountingDataSource,
    SpecialistAnalystStub,
)
from tests.integration.test_call_budget_store import prepare, reserve
from tests.integration.test_fixed_task_graph import context
from tests.task_facts_helpers import BASE_TIME, TaskClock, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class BlockedFrozenSource(CountingDataSource):
    def __init__(self):
        super().__init__()
        self.gates = {kind: BlockedQuery() for kind in EvidenceKind}

    def query_logs(self, **kwargs):
        self.gates[EvidenceKind.LOG]()
        return super().query_logs(**kwargs)

    def query_metrics(self, **kwargs):
        self.gates[EvidenceKind.METRIC]()
        return super().query_metrics(**kwargs)

    def list_recent_changes(self, **kwargs):
        self.gates[EvidenceKind.CHANGE]()
        return super().list_recent_changes(**kwargs)


class RecordingAnalyst(SpecialistAnalystStub):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def analyze(self, **kwargs):
        self.calls.append(kwargs["specialist"])
        return await super().analyze(**kwargs)


@pytest.mark.parametrize("mode", ["success", "timeout", "cancel", "lease_lost"])
async def test_graph_reserves_before_thread_and_rejects_late_results(
    database_session_factory, checkpoint_database_url, mode
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, claim = await claimed_run(database_session_factory, workflow_version=WorkflowVersion.V2)
    clock = TaskClock()
    source, analyst = BlockedFrozenSource(), RecordingAnalyst()
    executor = ReadOnlyQueryExecutor(timeout_seconds=2)
    budgets = CallBudgetQueries(database_session_factory)
    facts = CollaborationQueries(database_session_factory)
    async with open_checkpoint_saver(checkpoint_database_url) as saver:
        graph = TaskGraphFactory(
            session_factory=database_session_factory,
            data_source=source,
            analyst=analyst,
            synthesizer=ConclusionSynthesizerStub(),
            checkpointer=saver,
            clock=clock,
            query_executor=executor,
        )(context(claim))
        task = asyncio.create_task(
            graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        )
        try:
            await asyncio.wait_for(
                asyncio.gather(*(g.entered.wait() for g in source.gates.values())), 3
            )
            before = await budgets.get_snapshot(claim.run.id)
            assert before.reserved_tool_calls == 3 and before.reserved_model_calls == 0
            assert all(c.status == "reserved" and c.task_attempt_id for c in before.calls)
            assert analyst.calls == []
            if mode == "success":
                await drain(executor, *source.gates.values())
                result = await task
                assert len(result.specialist_reports) == 3
                after = await budgets.get_snapshot(claim.run.id)
                assert after.reserved_model_calls == 4 and after.reserved_tool_calls == 3
                assert all(c.status == "succeeded" for c in after.calls)
                return
            if mode == "cancel":
                task.cancel()
            if mode == "lease_lost":
                clock.current = BASE_TIME + timedelta(seconds=40)
                await drain(executor, *source.gates.values())
            expected = {
                "timeout": EvidenceQueryTimeout,
                "cancel": asyncio.CancelledError,
                "lease_lost": TaskFactConflict,
            }[mode]
            with pytest.raises(expected):
                await task
            if mode == "lease_lost":
                replacement = await parent.claim_next(
                    worker_id="replacement",
                    occurred_at=clock(),
                    lease_duration=timedelta(seconds=30),
                )
                assert replacement is not None
            else:
                # 真实线程尚未结束，下一次取证不能借取消获得新槽位。
                with pytest.raises(EvidenceQueryCapacityExceeded):
                    await executor.execute(lambda: None)
                await parent.fail(
                    claim.work_item.id,
                    lease_token=claim.work_item.lease_token,
                    occurred_at=clock(),
                    error=WorkItemError(error_code="test_stop", message="模拟父收尾"),
                    retry_delay=timedelta(0),
                )
            await drain(executor, *source.gates.values())
            after = await budgets.get_snapshot(claim.run.id)
            assert after.reserved_tool_calls == 3 and after.reserved_model_calls == 0
            assert all(c.status == "unknown" for c in after.calls)
            assert analyst.calls == []
            snapshot = await facts.get_snapshot(claim.run.id)
            for professional in snapshot.rounds[0].tasks:
                assert professional.status != "succeeded"
                assert await facts.get_result(claim.run.id, professional.id) is None
                assert await facts.get_evidence(claim.run.id, professional.id) is None
        finally:
            await drain(executor, *source.gates.values())
            executor.close()
            await asyncio.gather(task, return_exceptions=True)


async def test_budget_rejection_happens_before_any_thread_query(database_session_factory):
    _, claim, _, _, attempts, store = await prepare(
        database_session_factory, CallBudgetPolicy(max_tool_calls=1)
    )
    await reserve(store, claim, attempts["log"], CallOperation.QUERY_LOGS)
    runner = BudgetedCallRunner(
        run_id=claim.run.id, lease_token=claim.work_item.lease_token, store=store
    )
    executor = ReadOnlyQueryExecutor()
    gate = BlockedQuery()

    async def query():
        return await executor.execute(gate)

    try:
        metric = attempts["metric"]
        with runner.task_scope(metric.task_id, metric.id), pytest.raises(CallBudgetExceeded):
            await runner.execute(CallOperation.QUERY_METRICS, query)
        assert gate.calls == 0 and executor._pending == 0
    finally:
        await drain(executor, gate)
        executor.close()
