"""真实 PostgreSQL 与 checkpoint 验证第三轮计划持久化和恢复。"""

import asyncio
import json
from datetime import timedelta
from pathlib import Path

import httpx2
import pytest
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.coordination.persistence import CoordinationPlanStore
from opsswarm.investigation.coordination.queries import CoordinationPlanQueries
from opsswarm.investigation.models import EvidenceKind, InvestigationRunStatus, WorkItemError
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review_routing.queries import ReviewRoutingQueries
from opsswarm.investigation.tasks.contracts import RoundStatus, TaskStatus
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.dynamic_graph import DynamicThirdRoundPlanningGraphFactory
from tests.integration.test_dynamic_first_round_graph import (
    RecordingFrozenSource,
    processor_context,
)
from tests.integration.test_dynamic_second_round_review_graph import SecondRoundReviewReplies
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import sdk_model
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def migration_config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


class SecondRoundRoutingReplies(SecondRoundReviewReplies):
    """首轮固定补查，第二轮按测试场景返回审查意见。"""

    def __init__(self, db, roles, *, second_route_case: str) -> None:
        super().__init__(db, roles, route_case="follow_up")
        self.second_route_case = second_route_case

    async def __call__(self, request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        if "working_summaries" in data and "open_hypothesis" not in data:
            self.route_case = (
                "follow_up" if len(self.synthesis_inputs) == 0 else self.second_route_case
            )
        elif "open_hypothesis" in data:
            self.route_case = (
                "follow_up" if len(self.review_inputs) == 0 else self.second_route_case
            )
        return await super().__call__(request)


async def current_v11_claim(database_session_factory):
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v11")
        )
    claim = await store.claim_next(
        worker_id="dynamic-v11-third-round-planning-worker",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert claim is not None
    assert claim.run.graph_version == "dynamic-investigation-graph-v11"
    return store, claim


def planning_factory(db, source, client, saver, clock):
    return DynamicThirdRoundPlanningGraphFactory(
        session_factory=db,
        data_source=source,
        model=sdk_model(client),
        checkpointer=saver,
        clock=clock,
    )


@pytest.mark.parametrize(
    ("second_route_case", "expected_route", "expected_round_count"),
    [
        ("follow_up", "follow_up", 3),
        ("human", "await_human", 2),
        ("candidate", "candidate_evaluation", 2),
    ],
)
async def test_second_round_route_only_creates_third_round_for_follow_up(
    database_session_factory,
    checkpoint_database_url,
    second_route_case,
    expected_route,
    expected_round_count,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v11_claim(database_session_factory)
    source = RecordingFrozenSource(claim.request)
    replies = SecondRoundRoutingReplies(
        database_session_factory,
        (EvidenceKind.LOG, EvidenceKind.METRIC),
        second_route_case=second_route_case,
    )

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = planning_factory(database_session_factory, source, client, saver, TaskClock())(
            processor_context(claim)
        )
        result = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        assert await graph.resume(thread_id=claim.run.graph_thread_id) == result
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": claim.run.graph_thread_id}}
        )

    routes = await ReviewRoutingQueries(database_session_factory).list_for_run(claim.run.id)
    plans = await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert len(snapshot.rounds) == expected_round_count
    assert len(plans) == expected_round_count
    assert len(routes) == 2
    assert [item.decision.round_number for item in routes] == [1, 2]
    assert routes[0] == result.review_route
    assert routes[1] == result.follow_up_review_route
    assert routes[0].decision.route == "follow_up"
    assert routes[1].decision.route == expected_route
    assert routes[1].decision.existing_task_count == sum(
        len(round_record.tasks) for round_record in snapshot.rounds[:2]
    )
    assert routes[1].hypothesis_review_id == result.follow_up_hypothesis_review.id
    assert routes[1].open_hypothesis_id == result.follow_up_hypothesis.id

    if second_route_case == "follow_up":
        third_round = snapshot.rounds[2]
        third_plan = plans[2]
        assert result.third_round_plan == third_plan
        assert third_plan.round_id == third_round.id
        assert third_plan.source_call_id is None
        assert third_plan.source_review_route_id == routes[1].id
        assert third_plan.plan.assignment.round_number == 3
        assert third_round.status is RoundStatus.RUNNING
        assert all(task.status is TaskStatus.PENDING for task in third_round.tasks)
        assert tuple(task.assignment for task in third_round.tasks) == (
            third_plan.plan.assignment.tasks
        )
        assert {task.assignment.evidence_snapshot_id for task in third_round.tasks} == {
            source.snapshot_id
        }
        assert {task.assignment.window_end for task in third_round.tasks} == {
            snapshot.rounds[1].tasks[0].assignment.window_end
        }
    else:
        assert result.third_round_plan is None

    assert budget.reserved_model_calls == 11
    assert checkpoint is not None
    assert checkpoint.metadata["graph_version"] == "dynamic-investigation-graph-v11"
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None


@pytest.mark.parametrize("first_route_case", ["human", "candidate"])
async def test_first_round_terminal_routes_do_not_create_later_rounds(
    database_session_factory,
    checkpoint_database_url,
    first_route_case,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v11_claim(database_session_factory)
    source = RecordingFrozenSource(claim.request)
    replies = SecondRoundReviewReplies(
        database_session_factory,
        (EvidenceKind.LOG,),
        route_case=first_route_case,
    )

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = planning_factory(database_session_factory, source, client, saver, TaskClock())(
            processor_context(claim)
        )
        result = await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    routes = await ReviewRoutingQueries(database_session_factory).list_for_run(claim.run.id)
    plans = await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    assert routes == (result.review_route,)
    assert result.follow_up_review_route is None
    assert result.third_round_plan is None
    snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    assert len(snapshot.rounds) == len(plans) == 1
    run = await store.get_run(claim.run.id)
    assert run.status is InvestigationRunStatus.RUNNING and run.result is None


@pytest.mark.parametrize("crash_after_save", [False, True])
async def test_third_round_plan_recovers_across_fact_commit_window(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
    crash_after_save,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, claim = await current_v11_claim(database_session_factory)
    clock = TaskClock()
    replies = SecondRoundRoutingReplies(
        database_session_factory,
        (EvidenceKind.LOG,),
        second_route_case="follow_up",
    )
    original = CoordinationPlanStore.create_follow_up_plan
    save_count = 0

    async def interrupt_third_plan(self, *args, **kwargs):
        nonlocal save_count
        save_count += 1
        if save_count == 2 and not crash_after_save:
            raise RuntimeError("模拟第三轮计划事实提交前中断")
        saved = await original(self, *args, **kwargs)
        if save_count == 2 and crash_after_save:
            raise RuntimeError("模拟第三轮计划事实提交后 checkpoint 前中断")
        return saved

    with monkeypatch.context() as patch:
        patch.setattr(CoordinationPlanStore, "create_follow_up_plan", interrupt_third_plan)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            graph = planning_factory(
                database_session_factory,
                RecordingFrozenSource(claim.request),
                client,
                saver,
                clock,
            )(processor_context(claim))
            with pytest.raises(RuntimeError, match="第三轮计划事实提交"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)

    before = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    plans_before = await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    assert len(plans_before) == (3 if crash_after_save else 2)
    await store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="第三轮计划恢复窗口中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await store.claim_next(
        worker_id="dynamic-v11-third-round-planning-recovery",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=30),
        supported_workflow_versions=(WorkflowVersion.V3,),
    )
    assert replacement is not None
    recovery_source = RecordingFrozenSource(replacement.request)
    async with (
        httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda _: pytest.fail("计划恢复不应再次调用模型"))
        ) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = planning_factory(
            database_session_factory,
            recovery_source,
            client,
            saver,
            clock,
        )(processor_context(replacement))
        result = await graph.resume(thread_id=replacement.run.graph_thread_id)

    plans = await CoordinationPlanQueries(database_session_factory).list_plans(claim.run.id)
    assert len(plans) == 3
    assert result.third_round_plan == plans[2]
    assert await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id) == before
    assert all(not recovery_source.calls[role] for role in EvidenceKind)


async def test_third_round_plan_fact_blocks_downgrade_without_v11_run(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    _, claim = await current_v11_claim(database_session_factory)
    replies = SecondRoundRoutingReplies(
        database_session_factory,
        (EvidenceKind.LOG,),
        second_route_case="follow_up",
    )
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        graph = planning_factory(
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
            .values(graph_version="dynamic-investigation-graph-v10")
        )

    with pytest.raises(DBAPIError, match="不能删除已保存的第三轮协调计划事实"):
        await asyncio.to_thread(
            command.downgrade,
            migration_config(),
            "0052_second_round_routing",
        )

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0087_reflow_coordination"
        )
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM investigation_coordination_plans plan "
                    "JOIN investigation_rounds round_row ON round_row.id=plan.round_id "
                    "WHERE plan.investigation_run_id=:run_id "
                    "AND round_row.round_number=3 "
                    "AND plan.source_review_route_id IS NOT NULL"
                ),
                {"run_id": claim.run.id},
            )
            == 1
        )
