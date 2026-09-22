"""真实 PostgreSQL 与 checkpoint 验证 v3 确定性审查路由。"""

from collections import Counter
from datetime import timedelta

import httpx2
import pytest
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.models import EvidenceKind, InvestigationRunStatus, WorkItemError
from opsswarm.investigation.review_routing.persistence import ReviewRoutingStore
from opsswarm.investigation.review_routing.queries import ReviewRoutingQueries
from opsswarm.investigation.review_routing.tables import InvestigationHypothesisReviewRouteRow
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import DynamicRoutingGraphFactory
from tests.integration.test_dynamic_first_round_graph import (
    RecordingFrozenSource,
    processor_context,
)
from tests.integration.test_dynamic_review_graph import ReviewReplies
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def current_v5_claim(database_session_factory):
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    claim = await store.claim_next(
        worker_id="dynamic-v5-routing-worker",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    return store, claim


def routing_factory(db, source, client, saver, clock):
    return DynamicRoutingGraphFactory(
        session_factory=db,
        data_source=source,
        model=sdk_model(client),
        checkpointer=saver,
        clock=clock,
    )


@pytest.mark.parametrize(
    ("route_case", "expected_route"),
    [
        ("candidate", "candidate_evaluation"),
        ("follow_up", "follow_up"),
        ("human", "await_human"),
    ],
)
async def test_dynamic_routing_persists_three_routes_without_advancing_run(
    database_session_factory,
    checkpoint_database_url,
    route_case,
    expected_route,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v5_claim(database_session_factory)
    roles = (EvidenceKind.LOG, EvidenceKind.METRIC)
    replies = ReviewReplies(database_session_factory, roles, route_case=route_case)
    source = RecordingFrozenSource(claim.request)

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = routing_factory(database_session_factory, source, client, saver, TaskClock())(
            processor_context(claim)
        )
        result = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        assert await graph.resume(thread_id=claim.run.graph_thread_id) == result
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": claim.run.graph_thread_id}}
        )

    assert result.review_route.decision.route == expected_route
    assert await ReviewRoutingQueries(database_session_factory).list_for_run(claim.run.id) == (
        result.review_route,
    )
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    operations = Counter(item.operation for item in budget.calls)
    assert operations[CallOperation.REVIEW] == 1
    assert operations[CallOperation.SYNTHESIZE] == 1
    assert budget.reserved_model_calls == 3 + 2 * len(roles)
    assert budget.reserved_tool_calls == 2 * len(roles)
    assert len(result.coordination_plan.plan.assignment.tasks) == len(roles)
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None
    assert checkpoint is not None
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v5"
    if route_case == "candidate":
        with pytest.raises(DBAPIError, match="审查路由事实不可更新或删除"):
            async with database_session_factory() as session, session.begin():
                await session.execute(
                    update(InvestigationHypothesisReviewRouteRow)
                    .where(InvestigationHypothesisReviewRouteRow.id == result.review_route.id)
                    .values(status="tampered")
                )


async def test_dynamic_routing_recomputes_when_fact_was_not_saved(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v5_claim(database_session_factory)
    roles = (EvidenceKind.LOG, EvidenceKind.CHANGE)
    clock = TaskClock()
    replies = ReviewReplies(database_session_factory, roles, route_case="candidate")

    async def crash_before_save(*args, **kwargs):
        raise RuntimeError("模拟路由事实保存前中断")

    with monkeypatch.context() as patch:
        patch.setattr(ReviewRoutingStore, "save", crash_before_save)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = routing_factory(
                database_session_factory,
                RecordingFrozenSource(claim.request),
                client,
                saver,
                clock,
            )(processor_context(claim))
            with pytest.raises(RuntimeError, match="路由事实保存前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert not await ReviewRoutingQueries(database_session_factory).list_for_run(claim.run.id)
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="路由事实保存前中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="dynamic-routing-recompute",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda _: pytest.fail("确定性路由恢复不应再次调用模型"))
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = routing_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert result.review_route.decision.route == "candidate_evaluation"
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)


async def test_dynamic_routing_reuses_fact_committed_before_checkpoint(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v5_claim(database_session_factory)
    roles = (EvidenceKind.LOG, EvidenceKind.METRIC)
    clock = TaskClock()
    replies = ReviewReplies(database_session_factory, roles, route_case="follow_up")
    original = ReviewRoutingStore.save
    crashed = False

    async def save_then_crash(self, *args, **kwargs):
        nonlocal crashed
        result = await original(self, *args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError("模拟路由事实提交后 checkpoint 前中断")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(ReviewRoutingStore, "save", save_then_crash)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = routing_factory(
                database_session_factory,
                RecordingFrozenSource(claim.request),
                client,
                saver,
                clock,
            )(processor_context(claim))
            with pytest.raises(RuntimeError, match="checkpoint 前中断"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    routes = await ReviewRoutingQueries(database_session_factory).list_for_run(claim.run.id)
    assert len(routes) == 1
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="路由 checkpoint 窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="dynamic-routing-fact-recovery",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(
                lambda _: pytest.fail("已提交路由事实恢复不应再次调用模型")
            )
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = routing_factory(database_session_factory, recovery_source, client, saver, clock)(
            processor_context(replacement)
        )
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    assert result.review_route == routes[0]
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)
