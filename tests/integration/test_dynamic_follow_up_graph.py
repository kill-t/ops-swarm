"""真实 PostgreSQL 与 checkpoint 验证 v3 第二轮补查计划。"""

from collections import Counter
from datetime import timedelta

import httpx2
import pytest
from sqlalchemy import update

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.coordination.persistence import CoordinationPlanStore
from opsswarm.investigation.coordination.queries import CoordinationPlanQueries
from opsswarm.investigation.models import EvidenceKind, InvestigationRunStatus, WorkItemError
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import DynamicFollowUpGraphFactory
from tests.integration.test_dynamic_first_round_graph import (
    RecordingFrozenSource,
    processor_context,
)
from tests.integration.test_dynamic_review_graph import ReviewReplies
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def current_v6_claim(database_session_factory):
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v6")
        )
    claim = await store.claim_next(
        worker_id="dynamic-v6-follow-up-worker",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    return store, claim


def follow_up_factory(db, source, client, saver, clock):
    return DynamicFollowUpGraphFactory(
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
async def test_dynamic_follow_up_only_creates_pending_second_round_for_follow_up(
    database_session_factory,
    checkpoint_database_url,
    route_case,
    expects_follow_up,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v6_claim(database_session_factory)
    roles = (EvidenceKind.LOG, EvidenceKind.CHANGE)
    replies = ReviewReplies(database_session_factory, roles, route_case=route_case)
    source = RecordingFrozenSource(claim.request)

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = follow_up_factory(database_session_factory, source, client, saver, TaskClock())(
            processor_context(claim)
        )
        result = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        assert await graph.resume(thread_id=claim.run.graph_thread_id) == result
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": claim.run.graph_thread_id}}
        )

    plans = await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    operations = Counter(item.operation for item in budget.calls)
    assert operations[CallOperation.COORDINATE] == 1
    assert operations[CallOperation.SYNTHESIZE] == 1
    assert operations[CallOperation.REVIEW] == 1
    assert checkpoint is not None
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v6"
    assert (result.follow_up_plan is not None) is expects_follow_up
    assert len(plans) == (2 if expects_follow_up else 1)
    assert len(snapshot.rounds) == (2 if expects_follow_up else 1)
    if expects_follow_up:
        first, second = snapshot.rounds
        assert first.status == "completed"
        assert second.status == "running"
        assert len(second.tasks) == 1
        task = second.tasks[0]
        assert task.status == "pending"
        assert task.version == 1 and task.attempt_count == 0 and task.attempts == ()
        assert task.assignment.role is EvidenceKind.METRIC
        assert task.assignment.goal == "补查依赖等待队列"
        assert task.assignment.evidence_snapshot_id == source.snapshot_id
        assert task.assignment.window_end == source.snapshot_time
        assert (task.assignment.window_end - task.assignment.window_start).total_seconds() == 900
        assert plans[1] == result.follow_up_plan
        assert plans[1].source_review_route_id == result.review_route.id
        assert plans[1].source_call_id is None
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None


async def test_dynamic_follow_up_reuses_plan_committed_before_checkpoint(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v6_claim(database_session_factory)
    roles = (EvidenceKind.LOG, EvidenceKind.METRIC)
    clock = TaskClock()
    replies = ReviewReplies(database_session_factory, roles, route_case="follow_up")
    original = CoordinationPlanStore.create_follow_up_plan
    crashed = False

    async def save_then_crash(self, *args, **kwargs):
        nonlocal crashed
        saved = await original(self, *args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError("模拟补查计划提交后 checkpoint 前中断")
        return saved

    with monkeypatch.context() as patch:
        patch.setattr(CoordinationPlanStore, "create_follow_up_plan", save_then_crash)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = follow_up_factory(
                database_session_factory,
                RecordingFrozenSource(claim.request),
                client,
                saver,
                clock,
            )(processor_context(claim))
            with pytest.raises(RuntimeError, match="checkpoint 前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    plans = await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    assert len(plans) == 2
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="补查计划 checkpoint 窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="dynamic-follow-up-plan-recovery",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("已提交补查计划恢复不应再次调用模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = follow_up_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert result.follow_up_plan == plans[1]
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
    snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    assert len(snapshot.rounds) == 2 and len(snapshot.rounds[1].tasks) == 1


async def test_dynamic_follow_up_recomputes_when_plan_was_not_saved(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v6_claim(database_session_factory)
    roles = (EvidenceKind.LOG, EvidenceKind.CHANGE)
    clock = TaskClock()
    replies = ReviewReplies(database_session_factory, roles, route_case="follow_up")

    async def crash_before_save(*args, **kwargs):
        raise RuntimeError("模拟补查计划保存前中断")

    with monkeypatch.context() as patch:
        patch.setattr(CoordinationPlanStore, "create_follow_up_plan", crash_before_save)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = follow_up_factory(
                database_session_factory,
                RecordingFrozenSource(claim.request),
                client,
                saver,
                clock,
            )(processor_context(claim))
            with pytest.raises(RuntimeError, match="保存前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert (
        len(await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)) == 1
    )
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="补查计划保存前中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="dynamic-follow-up-plan-recompute",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("确定性补查计划恢复不应再次调用模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = follow_up_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert result.follow_up_plan is not None
    assert (
        len(await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)) == 2
    )
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
