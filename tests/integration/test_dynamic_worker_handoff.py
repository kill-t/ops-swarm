"""真实 PostgreSQL、checkpoint 和 SDK 假传输验证 Worker 的终端交接。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.control_plane.app import create_app
from opsswarm.incidents import ActorType, IncidentActor, InvalidIncidentTransition
from opsswarm.investigation.attempts import ModelUsageTelemetry
from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.handoff.persistence import TerminalHandoffStore
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.review_routing.queries import ReviewRoutingQueries
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.planning.persistence import ActionPlanStore
from opsswarm.worker.dynamic_processor import DynamicInvestigationProcessor
from opsswarm.worker.runner import InvestigationWorker
from tests.integration.test_dynamic_first_round_graph import processor_context
from tests.integration.test_dynamic_review_graph import ReviewReplies
from tests.integration.test_dynamic_third_round_routing_graph import (
    ThirdRoundRoutingReplies,
)
from tests.loop_helpers import loop_policy
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def make_processor(db, client, saver, clock):
    from opsswarm.worker.dynamic_factory import CurrentDynamicGraphFactory
    from tests.model_budget_helpers import sdk_model

    return DynamicInvestigationProcessor(
        factory=CurrentDynamicGraphFactory(
            session_factory=db,
            model=sdk_model(client),
            checkpointer=saver,
            clock=clock,
        ),
        model_name="fake-budget",
    )


def make_worker(store, processor, handoffs, clock):
    return InvestigationWorker(
        store=store,
        processor=processor,
        worker_id="dynamic-worker",
        clock=clock,
        handoff_store=handoffs,
        model_name="fake-budget",
        lease_duration=timedelta(seconds=300),
        processing_timeout=timedelta(seconds=240),
    )


async def setup_run(db):
    return await started_run(
        db,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
        call_budget=CallBudgetPolicy(max_model_calls=16, max_duration_seconds=900),
    )


async def claim(store, clock):
    result = await store.claim_next(
        worker_id="manual-dynamic-worker",
        occurred_at=clock(),
        lease_duration=timedelta(seconds=300),
        supported_workflow_versions=(WorkflowVersion.V3,),
        model_name="fake-budget",
    )
    assert result is not None
    return result


async def submit(handoffs, claimed, outcome, clock, **changes):
    arguments = dict(
        lease_token=claimed.work_item.lease_token,
        source_review_route_id=outcome.source_review_route_id,
        source_sha256=outcome.source_sha256,
        model_name=outcome.model_name,
        usage=ModelUsageTelemetry(),
        clock=clock,
    )
    return await handoffs.handoff(claimed.work_item.id, **{**arguments, **changes})


@pytest.mark.parametrize(
    ("route_case", "status"),
    [
        ("human", "await_human"),
        ("candidate", "await_candidate"),
    ],
)
async def test_worker_hands_off_both_terminals_and_exposes_queries(
    database_session_factory,
    checkpoint_database_url,
    route_case,
    status,
    monkeypatch,
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await setup_run(db)
    handoffs = TerminalHandoffStore(db)
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    app = create_app()
    app.state.investigation_store = store
    app.state.action_plan_store = ActionPlanStore(db)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as api:
        url = f"/api/v1/investigations/{started.run.id}/terminal-handoff"
        assert (await api.get(url)).json() is None
        assert (
            await api.get(f"/api/v1/investigations/{uuid4()}/terminal-handoff")
        ).status_code == 404
        replies = ReviewReplies(db, (EvidenceKind.LOG,), route_case=route_case)
        async with (
            httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
            open_checkpoint_saver(checkpoint_database_url) as saver,
        ):
            worker = make_worker(store, make_processor(db, client, saver, clock), handoffs, clock)
            assert await worker.run_once()
            assert not await worker.run_once()
        run = await store.get_run(started.run.id)
        work = await store.get_work_item(started.work_item.id)
        attempts = await store.list_attempts(run.id)
        record = await handoffs.get(run.id)
        assert run.status == status
        assert run.result is run.metrics is run.completed_at is None
        assert work.status == "completed" and work.lease_token is work.locked_until is None
        assert work.locked_by is None
        assert attempts[0].status == "handed_off" and attempts[0].will_retry is False
        assert not attempts[0].usage.usage_complete
        assert record.investigation_attempt_id == attempts[0].id
        response = await api.get(url)
        assert response.status_code == 200
        assert response.json() == record.model_dump(mode="json")
        assert "lease_token" not in response.text
        assert (await api.get(f"/api/v1/investigations/{run.id}")).json()["status"] == status
        assert (await api.post(f"/api/v1/investigations/{run.id}/action-plans")).status_code == 409
        events = await CollaborationQueries(db).list_events(run.id)
        assert [event.event_type for event in events].count("run.handed_off") == 1
        assert events[-1].event_type == "run.handed_off"
        assert await CollaborationQueries(db).list_events(
            run.id, after_cursor=events[-2].cursor
        ) == (events[-1],)
        with pytest.raises(InvalidIncidentTransition):
            await store.start_investigation(
                run.incident_id,
                expected_version=2,
                occurred_at=clock(),
                actor=IncidentActor(actor_type=ActorType.HUMAN, actor_id="engineer"),
                reason="重复启动",
            )

        async def damaged_source(_):
            raise TaskFactConflict("handoff_integrity_violation", "测试损坏来源")

        monkeypatch.setattr(store, "get_terminal_handoff", damaged_source)
        damaged = await api.get(url)
        assert damaged.status_code == 409
        assert damaged.json()["error_code"] == "handoff_integrity_violation"


async def test_handoff_rejects_bad_sources_and_late_leases_then_replays_once(
    database_session_factory,
    checkpoint_database_url,
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await setup_run(db)
    clock = TaskClock()
    claimed = await claim(store, clock)
    handoffs = TerminalHandoffStore(db)
    replies = ReviewReplies(db, (EvidenceKind.LOG,), route_case="candidate")
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        outcome = await make_processor(db, client, saver, clock).process(processor_context(claimed))
    from tests.integration.test_dynamic_first_round_graph import RecordingFrozenSource
    from tests.integration.test_dynamic_third_round_routing_graph import routing_factory

    async def forbidden(_):
        raise AssertionError("不同快照恢复必须在模型调用前拒绝")

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(forbidden)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        incompatible = DynamicInvestigationProcessor(
            factory=routing_factory(
                db, RecordingFrozenSource(claimed.request), client, saver, clock
            ),
            model_name="fake-budget",
        )
        with pytest.raises(TaskFactConflict) as conflict:
            await incompatible.process(processor_context(claimed))
        assert conflict.value.code in {"coordination_plan_conflict", "task_assignment_mismatch"}
    _, foreign_started = await setup_run(db)
    foreign = await claim(store, clock)
    for changes in [{"source_sha256": "a" * 64}, {"source_review_route_id": uuid4()}]:
        with pytest.raises(TaskFactConflict, match="终端路由"):
            await submit(handoffs, claimed, outcome, clock, **changes)
    with pytest.raises(TaskFactConflict, match="终端路由"):
        await submit(handoffs, foreign, outcome, clock)
    for times in [[claimed.attempt.lease_expires_at], [clock(), claimed.attempt.lease_expires_at]]:
        ticks = iter(times)
        with pytest.raises(TaskFactConflict, match="租约无效"):
            await submit(handoffs, claimed, outcome, lambda ticks=ticks: next(ticks))
        assert await handoffs.get(started.run.id) is None
    from opsswarm.investigation.errors import WorkItemLeaseLost

    ticks = iter([clock(), clock(), claimed.attempt.lease_expires_at])
    with pytest.raises(WorkItemLeaseLost):
        await submit(handoffs, claimed, outcome, lambda: next(ticks))
    assert await handoffs.get(started.run.id) is None
    assert (await store.get_run(started.run.id)).status == "running"
    assert (await store.list_attempts(started.run.id))[0].status == "running"
    assert "run.handed_off" not in [
        event.event_type for event in await CollaborationQueries(db).list_events(started.run.id)
    ]
    budget = await CallBudgetQueries(db).get_snapshot(started.run.id)
    results = await asyncio.gather(*(submit(handoffs, claimed, outcome, clock) for _ in range(2)))
    assert results[0] == results[1] == await handoffs.get(started.run.id)
    assert await submit(handoffs, claimed, outcome, clock) == results[0]
    for changes in [
        {"lease_token": uuid4()},
        {"source_sha256": "b" * 64},
        {"model_name": "another-model"},
        {"usage": ModelUsageTelemetry(model_request_count=1)},
    ]:
        with pytest.raises(TaskFactConflict, match="重复命令"):
            await submit(handoffs, claimed, outcome, clock, **changes)
    assert await CallBudgetQueries(db).get_snapshot(started.run.id) == budget
    assert (await store.get_run(foreign_started.run.id)).status == "running"
    for statement in [
        "UPDATE investigation_terminal_handoffs SET source_sha256=source_sha256 "
        "WHERE investigation_run_id=:id",
        "DELETE FROM investigation_terminal_handoffs WHERE investigation_run_id=:id",
        "UPDATE investigation_runs SET status='running' WHERE id=:id",
        "UPDATE investigation_attempts SET status='succeeded' WHERE investigation_run_id=:id",
    ]:
        with pytest.raises(DBAPIError):
            async with db() as session, session.begin():
                await session.execute(text(statement), {"id": started.run.id})


@pytest.mark.parametrize("window", ["before_handoff", "after_handoff"])
async def test_worker_recovers_graph_to_handoff_commit_windows(
    database_session_factory,
    checkpoint_database_url,
    window,
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await setup_run(db)
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    captured = []

    class LostResponseHandoffs(TerminalHandoffStore):
        async def handoff(self, *args, **kwargs):
            captured.append((args, kwargs))
            if window == "after_handoff":
                await super().handoff(*args, **kwargs)
            raise ConnectionError("模拟业务提交边界进程退出")

    replies = ReviewReplies(db, (EvidenceKind.LOG,), route_case="human")
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        processor = make_processor(db, client, saver, clock)
        worker = make_worker(store, processor, LostResponseHandoffs(db), clock)
        with pytest.raises(ConnectionError):
            await worker.run_once()
    before = await CallBudgetQueries(db).get_snapshot(started.run.id)
    handoffs = TerminalHandoffStore(db)
    if window == "after_handoff":
        original = await handoffs.get(started.run.id)
        assert await handoffs.handoff(*captured[0][0], **captured[0][1]) == original
    else:
        assert await handoffs.get(started.run.id) is None
    clock.current += timedelta(seconds=301)

    async def forbidden(_):
        raise AssertionError("恢复已完成图不应重新调用 SDK")

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(forbidden)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        worker = make_worker(store, make_processor(db, client, saver, clock), handoffs, clock)
        assert await worker.run_once() is (window == "before_handoff")
    record = await handoffs.get(started.run.id)
    attempts = await store.list_attempts(started.run.id)
    if window == "before_handoff":
        assert [attempt.status for attempt in attempts] == ["lease_expired", "handed_off"]
        assert record.investigation_attempt_id == attempts[1].id
        with pytest.raises(TaskFactConflict):
            await handoffs.handoff(*captured[0][0], **captured[0][1])
    else:
        assert [attempt.status for attempt in attempts] == ["handed_off"]
    assert await CallBudgetQueries(db).get_snapshot(started.run.id) == before


async def test_three_round_worker_rejects_intermediate_routes_and_hands_off_final_round(
    database_session_factory,
    checkpoint_database_url,
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await setup_run(db)
    clock = TaskClock()
    claimed = await claim(store, clock)
    replies = ThirdRoundRoutingReplies(db, third_route_case="round_limit")
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        outcome = await make_processor(db, client, saver, clock).process(processor_context(claimed))
    routes = await ReviewRoutingQueries(db).list_for_run(started.run.id)
    assert len(routes) == 3
    handoffs = TerminalHandoffStore(db)
    for route in routes[:2]:
        with pytest.raises(TaskFactConflict, match="最高轮终端"):
            await submit(
                handoffs,
                claimed,
                outcome,
                clock,
                source_review_route_id=route.id,
                source_sha256=route.content_sha256,
            )
    result = await submit(handoffs, claimed, outcome, clock)
    assert result.source_review_route_id == routes[2].id
    assert result.destination == "await_human"
    assert routes[2].decision.remaining_rounds == 0


async def test_generic_complete_cannot_accept_old_root_cause_for_v3(database_session_factory):
    from tests.unit.investigation.test_worker_routing import completed_live_result

    db = database_session_factory
    store, _ = await setup_run(db)
    clock = TaskClock()
    claimed = await claim(store, clock)
    outcome = completed_live_result()
    with pytest.raises(ValueError, match="v3 必须提交终端交接"):
        await store.complete(
            claimed.work_item.id,
            lease_token=claimed.work_item.lease_token,
            occurred_at=clock(),
            model_name="fake-budget",
            result=outcome.result,
            evidence_items=outcome.evidence_items,
            specialist_reports=outcome.specialist_reports,
            hypothesis_challenge=outcome.hypothesis_challenge,
            metrics=None,
        )


async def test_claim_filters_old_dynamic_graphs_without_spending_attempts(database_session_factory):
    db = database_session_factory
    store, started = await setup_run(db)
    async with db() as session, session.begin():
        await session.execute(
            text(
                "UPDATE investigation_runs SET graph_version='dynamic-investigation-graph-v14' "
                "WHERE id=:id"
            ),
            {"id": started.run.id},
        )

    class Unused:
        async def process(self, context):
            raise AssertionError("历史 v3 不能交给新图处理器")

    worker = make_worker(store, Unused(), TerminalHandoffStore(db), TaskClock())
    assert not await worker.run_once()
    assert (await store.get_work_item(started.work_item.id)).attempt_count == 0
    assert await TerminalHandoffStore(db).get(started.run.id) is None


async def test_dynamic_worker_retries_model_failure_without_resetting_run_budget(
    database_session_factory,
    checkpoint_database_url,
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await setup_run(db)
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    handoffs = TerminalHandoffStore(db)

    async def unavailable(_):
        raise TimeoutError("模拟 SDK 首次请求失败")

    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(unavailable)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        worker = make_worker(store, make_processor(db, client, saver, clock), handoffs, clock)
        assert await worker.run_once()
    first = await CallBudgetQueries(db).get_snapshot(started.run.id)
    assert first.calls and all(call.status != "reserved" for call in first.calls)
    assert (await store.get_run(started.run.id)).status == "queued"
    assert await handoffs.get(started.run.id) is None
    clock.current += timedelta(seconds=6)
    replies = ReviewReplies(db, (EvidenceKind.LOG,), route_case="human")
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        worker = make_worker(store, make_processor(db, client, saver, clock), handoffs, clock)
        assert await worker.run_once()
    after = await CallBudgetQueries(db).get_snapshot(started.run.id)
    assert after.policy == first.policy and after.deadline == first.deadline
    assert {call.id for call in first.calls} <= {call.id for call in after.calls}
    assert len(after.calls) > len(first.calls)
    assert [attempt.status for attempt in await store.list_attempts(started.run.id)] == [
        "failed",
        "handed_off",
    ]
    assert await handoffs.get(started.run.id) is not None
