"""真实 PostgreSQL 与 checkpoint 验证 v3 第三轮专业任务执行。"""

import asyncio
import json
from collections import Counter
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
from alembic.config import Config
from langchain_openai.chat_models.base import OpenAIConnectionError
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.models import EvidenceKind, InvestigationRunStatus, WorkItemError
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.results import SpecialistTaskResult
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import DynamicThirdRoundGraphFactory
from tests.integration.test_dynamic_first_round_graph import (
    RecordingFrozenSource,
    processor_context,
)
from tests.integration.test_dynamic_third_round_planning_graph import SecondRoundRoutingReplies
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def migration_config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def current_v12_claim(database_session_factory, *, expanded_budget: bool = False):
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(max_output_tokens=40000 if expanded_budget else 24000),
        call_budget=(
            CallBudgetPolicy(max_model_calls=24, max_tool_calls=30) if expanded_budget else None
        ),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v12")
        )
    claim = await store.claim_next(
        worker_id="dynamic-v12-third-round-worker",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    assert claim.run.graph_version == "dynamic-investigation-graph-v12"
    return store, claim


def third_round_factory(db, source, client, saver, clock):
    return DynamicThirdRoundGraphFactory(
        session_factory=db,
        data_source=source,
        model=sdk_model(client),
        checkpointer=saver,
        clock=clock,
    )


@pytest.mark.parametrize(
    ("second_route_case", "expected_round_count", "expected_models", "expected_tools"),
    [
        ("follow_up", 3, 11, 6),
        ("human", 2, 9, 4),
        ("candidate", 2, 9, 4),
    ],
)
async def test_third_round_executes_only_persisted_follow_up_plan(
    database_session_factory,
    checkpoint_database_url,
    second_route_case,
    expected_round_count,
    expected_models,
    expected_tools,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v12_claim(database_session_factory)
    source = RecordingFrozenSource(claim.request)
    replies = SecondRoundRoutingReplies(
        database_session_factory,
        (EvidenceKind.LOG,),
        second_route_case=second_route_case,
    )

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = third_round_factory(database_session_factory, source, client, saver, TaskClock())(
            processor_context(claim)
        )
        result = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        assert await graph.resume(thread_id=claim.run.graph_thread_id) == result
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": claim.run.graph_thread_id}}
        )

    facts = CollaborationQueries(database_session_factory)
    snapshot = await facts.get_snapshot(claim.run.id)
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert len(snapshot.rounds) == expected_round_count
    assert len(result.third_round_results) == (1 if second_route_case == "follow_up" else 0)
    assert budget.reserved_model_calls == expected_models
    assert budget.reserved_tool_calls == expected_tools
    assert checkpoint is not None
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v12"

    if second_route_case == "follow_up":
        assert result.third_round_plan is not None
        third_round = snapshot.rounds[2]
        assert third_round.id == result.third_round_plan.round_id
        assert third_round.status == "completed" and len(third_round.tasks) == 1
        task = third_round.tasks[0]
        assert task.status == "succeeded" and task.attempt_count == 1
        assert task.assignment.role is EvidenceKind.METRIC
        assert task.assignment.evidence_snapshot_id == source.snapshot_id
        assert task.assignment.window_end == source.snapshot_time
        stored = await facts.get_result(claim.run.id, task.id)
        evidence = await facts.get_evidence(claim.run.id, task.id)
        summary = await facts.get_working_summary(claim.run.id, task.id)
        assert stored is not None and evidence is not None and summary is not None
        assert stored.result == result.third_round_results[0]
        assert stored.attempt_id == summary.attempt_id == task.attempts[0].id
        assert (
            tuple(
                SpecialistTaskResult.model_validate(item)
                for item in checkpoint.checkpoint["channel_values"]["third_round_results"]
            )
            == result.third_round_results
        )
    else:
        assert result.third_round_plan is None
        assert result.third_round_results == ()
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None


class MultiThirdRoundReplies(SecondRoundRoutingReplies):
    """生成两个第三轮任务，并可只让第三轮日志任务首次失败。"""

    def __init__(self, db, *, fail_third_log: bool) -> None:
        super().__init__(db, (EvidenceKind.CHANGE,), second_route_case="follow_up")
        self.facts = CollaborationQueries(db)
        self.fail_third_log = fail_third_log

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        if "open_hypothesis" in data and len(self.review_inputs) == 1:
            self.review_inputs.append(data)
            hypothesis = data["open_hypothesis"]["hypothesis"]
            return sdk_response(
                payload,
                {
                    "support_is_sufficient": False,
                    "rationale": "第二轮仍需同时补查日志和变更。",
                    "accepted_supporting_evidence_ids": [],
                    "counter_evidence_ids": hypothesis["counter_evidence_ids"],
                    "evidence_gaps": ["缺少依赖错误日志与发布变更"],
                    "competing_explanations": [],
                    "suggested_direction": "follow_up",
                    "follow_up_suggestions": [
                        {
                            "role": "log",
                            "question": "补查第三轮依赖错误日志",
                            "rationale": "确认异常调用路径",
                        },
                        {
                            "role": "change",
                            "question": "补查第三轮发布变更",
                            "rationale": "排除新发布影响",
                        },
                    ],
                },
            )
        assignment = data.get("task_context", {}).get("assignment", {})
        if (
            self.fail_third_log
            and data.get("specialist") == EvidenceKind.LOG.value
            and assignment.get("goal") == "补查第三轮依赖错误日志"
            and len(payload["messages"]) == 2
        ):
            run_id = data["task_context"]["investigation_run_id"]
            async with asyncio.timeout(15):
                while True:
                    snapshot = await self.facts.get_snapshot(run_id)
                    if len(snapshot.rounds) == 3 and any(
                        task.assignment.role is EvidenceKind.CHANGE and task.status == "succeeded"
                        for task in snapshot.rounds[2].tasks
                    ):
                        break
                    await asyncio.sleep(0.01)
            self.fail_third_log = False
            raise RuntimeError("模拟第三轮日志专业模型中断")
        return await super().__call__(request)


async def test_third_round_recovers_only_failed_parallel_task(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v12_claim(database_session_factory, expanded_budget=True)
    clock = TaskClock()
    source = RecordingFrozenSource(claim.request)
    first_replies = MultiThirdRoundReplies(database_session_factory, fail_third_log=True)

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(first_replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = third_round_factory(database_session_factory, source, client, saver, clock)(
            processor_context(claim)
        )
        with pytest.raises(OpenAIConnectionError):
            await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    facts = CollaborationQueries(database_session_factory)
    before_round = (await facts.get_snapshot(claim.run.id)).rounds[2]
    assert {task.assignment.role: task.status for task in before_round.tasks} == {
        EvidenceKind.CHANGE: "succeeded",
        EvidenceKind.LOG: "pending",
    }
    before_budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="第三轮专业节点中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="replacement-third-round-worker",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    recovery_replies = MultiThirdRoundReplies(database_session_factory, fail_third_log=False)
    recovery_replies.professionals.allow_coordination = False
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(recovery_replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = third_round_factory(
            database_session_factory, recovery_source, client, saver, clock
        )(processor_context(replacement))
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert {item.report.specialist for item in result.third_round_results} == {
        EvidenceKind.CHANGE,
        EvidenceKind.LOG,
    }
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
    after_round = (await facts.get_snapshot(claim.run.id)).rounds[2]
    assert after_round.status == "completed"
    assert {task.assignment.role: len(task.attempts) for task in after_round.tasks} == {
        EvidenceKind.CHANGE: 1,
        EvidenceKind.LOG: 2,
    }
    after_budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert after_budget.reserved_model_calls == before_budget.reserved_model_calls + 2
    assert after_budget.reserved_tool_calls == before_budget.reserved_tool_calls + 1
    events = await facts.list_events(claim.run.id)
    event_types = Counter(item.event_type for item in events)
    assert event_types["task.failed"] == 1
    assert event_types["round.completed"] == 3
    assert [item.cursor for item in events] == list(range(1, len(events) + 1))


async def test_third_round_reuses_result_committed_before_node_checkpoint(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v12_claim(database_session_factory)
    clock = TaskClock()
    source = RecordingFrozenSource(claim.request)
    replies = SecondRoundRoutingReplies(
        database_session_factory,
        (EvidenceKind.LOG,),
        second_route_case="follow_up",
    )
    facts = CollaborationQueries(database_session_factory)
    original = InvestigationTaskStore.finish_attempt
    crashed = False

    async def finish_then_crash(self, run_id, task_id, attempt_id, **kwargs):
        nonlocal crashed
        record = await original(self, run_id, task_id, attempt_id, **kwargs)
        if kwargs.get("result") is not None and not crashed:
            snapshot = await facts.get_snapshot(run_id)
            if len(snapshot.rounds) == 3 and any(
                task.id == task_id for task in snapshot.rounds[2].tasks
            ):
                crashed = True
                raise RuntimeError("模拟第三轮结果提交后 checkpoint 前中断")
        return record

    with monkeypatch.context() as patch:
        patch.setattr(InvestigationTaskStore, "finish_attempt", finish_then_crash)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = third_round_factory(database_session_factory, source, client, saver, clock)(
                processor_context(claim)
            )
            with pytest.raises(RuntimeError, match="checkpoint 前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before_round = (await facts.get_snapshot(claim.run.id)).rounds[2]
    before_budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    before_results = {
        task.id: await facts.get_result(claim.run.id, task.id) for task in before_round.tasks
    }
    before_summaries = {
        task.id: await facts.get_working_summary(claim.run.id, task.id)
        for task in before_round.tasks
    }
    assert before_round.status == "completed"
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="第三轮 checkpoint 窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="replacement-after-third-round-result",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("已提交第三轮结果恢复不应再次调用模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = third_round_factory(
            database_session_factory, recovery_source, client, saver, clock
        )(processor_context(replacement))
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert len(result.third_round_results) == 1
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
    after_round = (await facts.get_snapshot(claim.run.id)).rounds[2]
    assert all(task.attempt_count == 1 for task in after_round.tasks)
    assert {
        task.id: await facts.get_result(claim.run.id, task.id) for task in after_round.tasks
    } == before_results
    assert {
        task.id: await facts.get_working_summary(claim.run.id, task.id)
        for task in after_round.tasks
    } == before_summaries
    assert (
        await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
        == before_budget
    )


async def test_third_round_execution_fact_blocks_downgrade_without_v12_run(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    _, claim = await current_v12_claim(database_session_factory)
    replies = SecondRoundRoutingReplies(
        database_session_factory,
        (EvidenceKind.LOG,),
        second_route_case="follow_up",
    )
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = third_round_factory(
            database_session_factory,
            RecordingFrozenSource(claim.request),
            client,
            saver,
            TaskClock(),
        )(processor_context(claim))
        await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == claim.run.id)
            .values(graph_version="dynamic-investigation-graph-v11")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第三轮专业执行事实"):
        await asyncio.to_thread(
            command.downgrade,
            migration_config(),
            "0053_third_round_planning",
        )

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0078_condition_approval"
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM agent_task_attempts attempt "
                    "JOIN agent_tasks task ON task.id=attempt.task_id "
                    "JOIN investigation_rounds round_row ON round_row.id=task.round_id "
                    "WHERE task.investigation_run_id=:run_id AND round_row.round_number=3"
                ),
                {"run_id": claim.run.id},
            )
            == 1
        )
