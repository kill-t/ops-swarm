"""真实 PostgreSQL 与 checkpoint 验证 v3 第二轮专业任务执行。"""

import asyncio
import json
from collections import Counter
from datetime import timedelta

import httpx2
import pytest
from langchain_openai.chat_models.base import OpenAIConnectionError

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.models import EvidenceKind, InvestigationRunStatus, WorkItemError
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.results import SpecialistTaskResult
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import DynamicSecondRoundGraphFactory
from tests.integration.test_dynamic_first_round_graph import (
    RecordingFrozenSource,
    processor_context,
)
from tests.integration.test_dynamic_review_graph import ReviewReplies
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def current_v7_claim(database_session_factory):
    store, _ = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    claim = await store.claim_next(
        worker_id="dynamic-v7-second-round-worker",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    return store, claim


def second_round_factory(db, source, client, saver, clock):
    return DynamicSecondRoundGraphFactory(
        session_factory=db,
        data_source=source,
        model=sdk_model(client),
        checkpointer=saver,
        clock=clock,
    )


@pytest.mark.parametrize(
    ("route_case", "expects_follow_up"),
    [("follow_up", True), ("candidate", False), ("human", False)],
)
async def test_second_round_executes_only_persisted_follow_up_plan(
    database_session_factory,
    checkpoint_database_url,
    route_case,
    expects_follow_up,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v7_claim(database_session_factory)
    first_roles = (EvidenceKind.LOG, EvidenceKind.METRIC)
    replies = ReviewReplies(database_session_factory, first_roles, route_case=route_case)
    source = RecordingFrozenSource(claim.request)

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = second_round_factory(database_session_factory, source, client, saver, TaskClock())(
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
    assert checkpoint is not None
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v7"
    assert len(snapshot.rounds) == (2 if expects_follow_up else 1)
    assert len(result.follow_up_results) == (1 if expects_follow_up else 0)
    assert budget.reserved_model_calls == (9 if expects_follow_up else 7)
    assert budget.reserved_tool_calls == (6 if expects_follow_up else 4)
    expected_metric_calls = [(claim.request.service_name, 5)]
    if expects_follow_up:
        expected_metric_calls.append((claim.request.service_name, 15))
    assert source.calls[EvidenceKind.METRIC] == expected_metric_calls
    if expects_follow_up:
        second = snapshot.rounds[1]
        assert second.status == "completed" and len(second.tasks) == 1
        task = second.tasks[0]
        assert task.status == "succeeded" and task.attempt_count == 1
        assert task.assignment.role is EvidenceKind.METRIC
        assert task.assignment.goal == "补查依赖等待队列"
        assert task.assignment.evidence_snapshot_id == source.snapshot_id
        assert task.assignment.window_end == source.snapshot_time
        stored = await facts.get_result(claim.run.id, task.id)
        evidence = await facts.get_evidence(claim.run.id, task.id)
        summary = await facts.get_working_summary(claim.run.id, task.id)
        assert stored is not None and evidence is not None and summary is not None
        assert stored.result == result.follow_up_results[0]
        assert stored.attempt_id == summary.attempt_id == task.attempts[0].id
        assert any(
            item.report.specialist is EvidenceKind.METRIC for item in result.specialist_results
        )
        assert result.follow_up_results[0].report.specialist is EvidenceKind.METRIC
        assert (
            tuple(
                SpecialistTaskResult.model_validate(item)
                for item in checkpoint.checkpoint["channel_values"]["follow_up_results"]
            )
            == result.follow_up_results
        )
    else:
        assert result.follow_up_plan is None
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None


class MultiFollowUpReplies:
    """生成两个第二轮任务，并可只让第二轮日志任务首次失败。"""

    def __init__(self, db, *, fail_second_log: bool) -> None:
        self.base = ReviewReplies(db, (EvidenceKind.CHANGE,), route_case="follow_up")
        self.facts = CollaborationQueries(db)
        self.fail_second_log = fail_second_log

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        if "working_summaries" in data and "open_hypothesis" in data:
            hypothesis = data["open_hypothesis"]["hypothesis"]
            return sdk_response(
                payload,
                {
                    "support_is_sufficient": False,
                    "rationale": "当前证据不足，需要同时补查日志与指标。",
                    "accepted_supporting_evidence_ids": [],
                    "counter_evidence_ids": hypothesis["counter_evidence_ids"],
                    "evidence_gaps": ["缺少依赖错误日志与等待队列指标"],
                    "competing_explanations": [],
                    "suggested_direction": "follow_up",
                    "follow_up_suggestions": [
                        {
                            "role": "log",
                            "question": "补查依赖错误日志",
                            "rationale": "确认异常调用路径",
                        },
                        {
                            "role": "metric",
                            "question": "补查依赖等待队列",
                            "rationale": "区分容量耗尽与应用异常",
                        },
                    ],
                },
            )
        assignment = data.get("task_context", {}).get("assignment", {})
        if (
            self.fail_second_log
            and data.get("specialist") == EvidenceKind.LOG.value
            and assignment.get("goal") == "补查依赖错误日志"
            and len(payload["messages"]) == 2
        ):
            run_id = data["task_context"]["investigation_run_id"]
            async with asyncio.timeout(15):
                while True:
                    snapshot = await self.facts.get_snapshot(run_id)
                    if len(snapshot.rounds) == 2 and any(
                        task.assignment.role is EvidenceKind.METRIC and task.status == "succeeded"
                        for task in snapshot.rounds[1].tasks
                    ):
                        break
                    await asyncio.sleep(0.01)
            self.fail_second_log = False
            raise RuntimeError("模拟第二轮日志专业模型中断")
        return await self.base(request)


async def test_second_round_recovers_only_failed_parallel_task(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v7_claim(database_session_factory)
    clock = TaskClock()
    source = RecordingFrozenSource(claim.request)
    first_replies = MultiFollowUpReplies(database_session_factory, fail_second_log=True)

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(first_replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = second_round_factory(database_session_factory, source, client, saver, clock)(
            processor_context(claim)
        )
        with pytest.raises(OpenAIConnectionError):
            await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    facts = CollaborationQueries(database_session_factory)
    before_round = (await facts.get_snapshot(claim.run.id)).rounds[1]
    assert {task.assignment.role: task.status for task in before_round.tasks} == {
        EvidenceKind.LOG: "pending",
        EvidenceKind.METRIC: "succeeded",
    }
    before_budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="第二轮专业节点中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="replacement-second-round-worker",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    recovery_replies = MultiFollowUpReplies(database_session_factory, fail_second_log=False)
    recovery_replies.base.professionals.allow_coordination = False
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(recovery_replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = second_round_factory(
            database_session_factory, recovery_source, client, saver, clock
        )(processor_context(replacement))
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert {item.report.specialist for item in result.follow_up_results} == {
        EvidenceKind.LOG,
        EvidenceKind.METRIC,
    }
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
    assert recovery_replies.base.professionals.seen[claim.run.id] == [
        EvidenceKind.LOG,
        EvidenceKind.LOG,
    ]
    after_round = (await facts.get_snapshot(claim.run.id)).rounds[1]
    assert after_round.status == "completed"
    assert {task.assignment.role: len(task.attempts) for task in after_round.tasks} == {
        EvidenceKind.LOG: 2,
        EvidenceKind.METRIC: 1,
    }
    after_budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert after_budget.reserved_model_calls == before_budget.reserved_model_calls + 2
    assert after_budget.reserved_tool_calls == before_budget.reserved_tool_calls + 1
    events = await facts.list_events(claim.run.id)
    event_types = Counter(item.event_type for item in events)
    assert event_types["task.failed"] == 1
    assert event_types["round.completed"] == 2
    assert [item.cursor for item in events] == list(range(1, len(events) + 1))


async def test_second_round_reuses_results_committed_before_node_checkpoint(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v7_claim(database_session_factory)
    clock = TaskClock()
    source = RecordingFrozenSource(claim.request)
    replies = ReviewReplies(database_session_factory, (EvidenceKind.LOG,), route_case="follow_up")
    facts = CollaborationQueries(database_session_factory)
    original = InvestigationTaskStore.finish_attempt
    crashed = False

    async def finish_then_crash(self, run_id, task_id, attempt_id, **kwargs):
        nonlocal crashed
        record = await original(self, run_id, task_id, attempt_id, **kwargs)
        if kwargs.get("result") is not None and not crashed:
            snapshot = await facts.get_snapshot(run_id)
            if len(snapshot.rounds) == 2 and any(
                task.id == task_id for task in snapshot.rounds[1].tasks
            ):
                crashed = True
                raise RuntimeError("模拟第二轮结果提交后 checkpoint 前中断")
        return record

    with monkeypatch.context() as patch:
        patch.setattr(InvestigationTaskStore, "finish_attempt", finish_then_crash)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = second_round_factory(database_session_factory, source, client, saver, clock)(
                processor_context(claim)
            )
            with pytest.raises(RuntimeError, match="checkpoint 前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before_round = (await facts.get_snapshot(claim.run.id)).rounds[1]
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
        error=WorkItemError(error_code="crash", message="第二轮 checkpoint 窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="replacement-after-second-round-result",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("已提交第二轮结果恢复不应再次调用模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = second_round_factory(
            database_session_factory, recovery_source, client, saver, clock
        )(processor_context(replacement))
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert len(result.follow_up_results) == 1
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
    after_round = (await facts.get_snapshot(claim.run.id)).rounds[1]
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
