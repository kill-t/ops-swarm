"""真实重评、PostgreSQL 路由事实、可信读取及原图隔离。"""

import asyncio
import copy
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

import opsswarm.investigation.human_review.job_store as review_jobs
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_prepare.tables import HumanPreparationJobRow
from opsswarm.investigation.human_review.contracts import HumanReviewError
from opsswarm.investigation.human_review.job_store import HumanReviewJobStore
from opsswarm.investigation.human_review.ledger import HumanReviewLedger
from opsswarm.investigation.human_review.tables import HumanReviewResultRow
from opsswarm.investigation.human_routing.contracts import HumanRoutingRecord
from opsswarm.investigation.human_routing.store import HumanRoutingStore
from opsswarm.investigation.human_routing.tables import HumanReviewRouteRow
from tests.api.test_human_reassessment_api import (
    RecordingModel,
    count,
    get,
    prepared_context,
    reassess,
    review_result,
    worker,
)
from tests.api.test_human_wait_api import command, post
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.identity_helpers import auth_header

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def route(c, job_id, *, header=None, preparation_id=None):
    base = (
        c.review_base
        if preparation_id is None
        else f"{c.base}/preparations/{preparation_id}/reassessments"
    )
    return await c.client.get(f"{base}/{job_id}/route", headers=header or auth_header("viewer"))


async def proposal(c, direction):
    async with c.db() as session:
        preparation = await session.get(HumanPreparationJobRow, UUID(c.preparation_id))
        material = json.loads(preparation.source["prior_input"]["messages"][1]["content"])
    if direction == "candidate":
        return review_result(
            support_is_sufficient=True,
            accepted_supporting_evidence_ids=material["open_hypothesis"]["hypothesis"][
                "supporting_evidence_ids"
            ][:1],
            evidence_gaps=(),
            suggested_direction="evaluate_for_routing",
        )
    return review_result(
        suggested_direction="follow_up",
        follow_up_suggestions=(
            {
                "role": "metric",
                "question": "补查隔离演练依赖指标",
                "rationale": "核对证据缺口",
            },
        ),
    )


@pytest.mark.parametrize("wait_context", ["human_clean"], indirect=True)
@pytest.mark.parametrize(
    ("direction", "constraints", "expected"),
    [
        ("candidate", False, "candidate_evaluation"),
        ("follow_up", False, "follow_up"),
        ("candidate", True, "await_human"),
    ],
)
async def test_success_persists_exact_intent_without_dispatch(
    wait_context, direction, constraints, expected
):
    c = wait_context
    await prepared_context(
        c, items=None if constraints else [{"kind": "statement", "text": "补充材料仍需证据核验"}]
    )
    created = await reassess(c)
    job_id = created.json()["job"]["id"]
    old_work = await c.store.get_work_item(c.started.work_item.id)
    old_attempts = await c.store.list_attempts(c.started.run.id)
    assert (await route(c, job_id)).json()["availability"] == "pending"
    model = RecordingModel(result=await proposal(c, direction))
    assert await worker(c, model).run_once()
    review = (await get(c, job_id)).json()
    response = await route(c, job_id)
    assert response.status_code == 200, response.text
    view = response.json()
    saved = HumanRoutingRecord.model_validate(view["record"])
    assert view["availability"] == "current" and view["unavailable_reason"] is None
    assert saved.result_sha256 == review["result"]["content_sha256"]
    assert str(saved.source_call_id) == review["result"]["source_call_id"]
    assert saved.decision.route == expected
    assert saved.decision.dispatch_status == "not_dispatched"
    assert content_hash(saved.decision) == saved.content_sha256
    assert saved.decision.base_decision.round_number == 1
    assert saved.decision.budget == await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    if constraints:
        assert saved.decision.blocking_reason_codes == ("human_constraints_unresolved",)
        assert saved.decision.unresolved_constraints[0].text == "本轮只读，不允许执行回滚。"
    assert await c.store.get_work_item(c.started.work_item.id) == old_work
    assert await c.store.list_attempts(c.started.run.id) == old_attempts
    assert (await c.store.get_run(c.started.run.id)).status == "await_human"
    assert await count(c, HumanReviewRouteRow) == 1
    assert not await worker(c, model).run_once() and len(model.inputs) == 1
    path = f"{c.review_base}/{job_id}/route"
    assert (await c.client.get(path)).status_code == 401
    assert (await route(c, uuid4())).status_code == 404
    assert (await route(c, job_id, preparation_id=uuid4())).status_code == 404
    assert (await c.client.post(path, json={}, headers=auth_header("engineer"))).status_code == 405
    c.clock.current += timedelta(seconds=901)
    later = (await route(c, job_id)).json()
    assert later["record"] == view["record"]
    assert "run_deadline_exceeded" in later["current_blocking_reason_codes"]
    assert await count(c, HumanReviewRouteRow) == 1


@pytest.mark.parametrize("wait_context", ["three_round"], indirect=True)
async def test_third_round_reassessment_cannot_create_fourth_round(wait_context):
    c = wait_context
    await prepared_context(c, items=[{"kind": "question", "text": "能否继续补查？"}])
    created = await reassess(c)
    job_id = created.json()["job"]["id"]
    model = RecordingModel(result=await proposal(c, "follow_up"))
    assert await worker(c, model).run_once()
    response = await route(c, job_id)
    assert response.status_code == 200, response.text
    saved = response.json()["record"]["decision"]
    assert saved["route"] == "await_human"
    assert saved["base_decision"]["round_number"] == 3
    assert saved["base_decision"]["remaining_rounds"] == 0
    assert "round_limit_reached" in saved["base_decision"]["reason_codes"]
    assert (await c.store.get_run(c.started.run.id)).status == "await_human"
    assert saved["budget"]["reserved_model_calls"] == 14


async def old_success(c, monkeypatch, *, items=None):
    await prepared_context(c, items=items)
    created = await reassess(c)
    job_id = created.json()["job"]["id"]

    async def without_route(*args, **kwargs):
        return False

    with monkeypatch.context() as patch:
        patch.setattr(review_jobs, "record_route", without_route)
        assert await worker(c, RecordingModel()).run_once()
    assert (await get(c, job_id)).json()["status"] == "succeeded"
    assert await count(c, HumanReviewRouteRow) == 0
    return job_id


async def test_old_success_backfill_is_concurrent_idempotent_and_never_calls_model(
    wait_context, monkeypatch
):
    c = wait_context
    job_id = await old_success(c, monkeypatch)
    budget = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    c.clock.current += timedelta(seconds=901)
    routes = HumanRoutingStore(c.db, clock=c.clock)
    results = await asyncio.gather(routes.record_next(), routes.record_next())
    assert sorted(results) == [False, True]
    saved = (await route(c, job_id)).json()["record"]
    assert "run_deadline_exceeded" in saved["decision"]["blocking_reason_codes"]
    assert saved["created_at"] != (await get(c, job_id)).json()["result"]["created_at"]
    assert not await routes.record_next()
    assert await CallBudgetQueries(c.db).get_snapshot(c.started.run.id) == budget
    assert await count(c, HumanReviewRouteRow) == 1


@pytest.mark.parametrize("action", ["supplement", "cancel"])
async def test_input_change_keeps_old_route_but_prevents_backfill(
    wait_context, monkeypatch, action
):
    c = wait_context
    job_id = await old_success(c, monkeypatch)
    routes = HumanRoutingStore(c.db, clock=c.clock)
    # work 锁竞争时补齐不能等待占用者，也不能制造路由。
    async with c.db() as session, session.begin():
        await session.execute(
            text("SELECT id FROM work_items WHERE investigation_run_id=:id FOR UPDATE"),
            {"id": c.started.run.id},
        )
        assert not await asyncio.wait_for(routes.record_next(), 5)
    assert await routes.record_next()
    saved = (await route(c, job_id)).json()["record"]
    assert (
        await post(c, command(c, action=action, version=2, key="changed-input-115"))
    ).status_code == 201
    historical = (await route(c, job_id)).json()
    assert historical["availability"] == "stale" and historical["record"] == saved
    assert not await routes.record_next()
    assert await count(c, HumanReviewRouteRow) == 1


async def test_route_flush_failure_rolls_back_result_and_retry_reuses_successful_call(
    wait_context, monkeypatch
):
    c = wait_context
    await prepared_context(c)
    created = await reassess(c)
    job_id = created.json()["job"]["id"]
    jobs, ledger = HumanReviewJobStore(c.db, clock=c.clock), HumanReviewLedger(c.db, clock=c.clock)
    claim = await jobs.claim_next(worker_id="atomic-route")
    grant = await ledger.reserve(claim)
    assert await ledger.settle(
        claim, grant.record.id, status="succeeded", result=review_result(), usage=None
    )

    def fail_flush(session, flush_context):
        if any(isinstance(row, HumanReviewRouteRow) for row in session.new):
            raise RuntimeError("模拟路由保存后崩溃")

    # flush 后再失败，验证已经写到数据库的意见、路由及成功状态一起回滚。
    sync_session = AsyncSession.sync_session_class
    event.listen(sync_session, "after_flush", fail_flush)
    try:
        with pytest.raises(RuntimeError, match="路由"):
            await jobs.complete(claim, grant.record.id)
    finally:
        event.remove(sync_session, "after_flush", fail_flush)
    assert await count(c, HumanReviewResultRow) == 0 and await count(c, HumanReviewRouteRow) == 0
    assert (await get(c, job_id)).json()["status"] == "running"
    await jobs.fail(claim, error_code="simulated_crash", retryable=True)
    c.clock.current += timedelta(seconds=3)
    model = RecordingModel()
    assert await worker(c, model).run_once() and model.inputs == []
    assert (await route(c, job_id)).json()["availability"] == "current"


async def test_native_source_guards_immutable_fact_and_query_recompilation(
    wait_context, monkeypatch
):
    c = wait_context
    job_id = await old_success(c, monkeypatch, items=[{"kind": "question", "text": "请保留缺口"}])
    # 先在回滚事务内形成一份有效样本，再逐项攻击原生插入护栏。
    from opsswarm.investigation.human_review.tables import HumanReviewJobRow
    from opsswarm.investigation.human_routing.persistence import record_route
    from opsswarm.investigation.human_wait.state import lock_wait_run

    async with c.db() as session, session.begin():
        run = await lock_wait_run(session, c.started.run.id, read=False)
        job = await session.get(HumanReviewJobRow, UUID(job_id))
        assert await record_route(session, job, run, clock=c.clock)
        original = await session.scalar(select(HumanReviewRouteRow))
        values = {
            column.name: copy.deepcopy(getattr(original, column.name))
            for column in HumanReviewRouteRow.__table__.columns
        }
        await session.rollback()
    mutations = [
        {"investigation_run_id": uuid4()},
        {"wait_version": 3},
        {"preparation_source_sha256": "a" * 64},
        {"result_sha256": "a" * 64},
        {"source_call_id": uuid4()},
        {"created_at": values["created_at"] - timedelta(seconds=1)},
    ]
    for missing in [None, -1]:
        invalid = copy.deepcopy(values["decision"])
        invalid["route"] = "follow_up"
        invalid["base_decision"]["route"] = "follow_up"
        invalid["base_decision"]["follow_up_suggestions"] = None
        invalid["minimum_model_calls"] = missing
        invalid["minimum_tool_calls"] = missing
        mutations.append({"decision": invalid})
    for changes in mutations:
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                session.add(HumanReviewRouteRow(**{**values, **changes}))
                await session.flush()
    assert await HumanRoutingStore(c.db, clock=c.clock).record_next()
    from pathlib import Path

    from alembic.config import Config

    from alembic import command as migration_command

    with pytest.raises(Exception, match="不能删除已保存的人工重评路由"):
        await asyncio.to_thread(
            migration_command.downgrade,
            Config(str(Path(__file__).resolve().parents[2] / "alembic.ini")),
            "0070_human_input_reassessment",
        )
    for operation in [
        "UPDATE investigation_human_review_routes SET result_sha256=repeat('b',64)",
        "DELETE FROM investigation_human_review_routes",
    ]:
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text(operation))
    async with c.db() as session, session.begin():
        await session.execute(
            text(
                "ALTER TABLE investigation_human_review_routes "
                "DISABLE TRIGGER human_review_routes_guard"
            )
        )
        await session.execute(
            text("UPDATE investigation_human_review_routes SET content_sha256=repeat('b',64)")
        )
        await session.execute(
            text(
                "ALTER TABLE investigation_human_review_routes "
                "ENABLE TRIGGER human_review_routes_guard"
            )
        )
    response = await route(c, job_id)
    assert (
        response.status_code == 409
        and response.json()["error_code"] == "human_routing_integrity_violation"
    )


@pytest.mark.parametrize("action", ["supplement", "cancel"])
async def test_changed_input_without_route_is_never_backfilled(wait_context, monkeypatch, action):
    c = wait_context
    job_id = await old_success(c, monkeypatch)
    assert (
        await post(c, command(c, action=action, version=2, key="pre-routing-change"))
    ).status_code == 201
    assert not await HumanRoutingStore(c.db, clock=c.clock).record_next()
    response = (await route(c, job_id)).json()
    assert response["record"] is None and response["availability"] == "stale"
    assert await count(c, HumanReviewRouteRow) == 0


async def test_source_rejection_is_observable_without_killing_worker(wait_context, monkeypatch):
    import opsswarm.investigation.human_routing.store as routing_module

    c = wait_context
    job_id = await old_success(c, monkeypatch)

    async def reject(*args, **kwargs):
        raise HumanReviewError("human_review_integrity_violation", "模拟不可变来源损坏")

    with monkeypatch.context() as patch:
        patch.setattr(routing_module, "record_route", reject)
        assert not await worker(c, RecordingModel()).run_once()
    assert await count(c, HumanReviewRouteRow) == 0
    assert await HumanRoutingStore(c.db, clock=c.clock).record_next()
    assert (await route(c, job_id)).json()["availability"] == "current"


async def test_incident_transition_waits_for_routing_transaction(wait_context, monkeypatch):
    import opsswarm.investigation.human_routing.persistence as persistence

    c = wait_context
    job_id = await old_success(c, monkeypatch)
    entered, release = asyncio.Event(), asyncio.Event()
    read = persistence.read_job_packet

    async def pause(*args, **kwargs):
        result = await read(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    async def transition():
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE incidents SET status='awaiting_approval', "
                    "version=version+1 WHERE id=:id"
                ),
                {"id": c.started.run.incident_id},
            )

    with monkeypatch.context() as patch:
        patch.setattr(persistence, "read_job_packet", pause)
        routing = asyncio.create_task(HumanRoutingStore(c.db, clock=c.clock).record_next())
        await asyncio.wait_for(entered.wait(), 30)
        changing = asyncio.create_task(transition())
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(changing), 0.2)
        finally:
            release.set()
        assert await routing
        await changing
    response = (await route(c, job_id)).json()
    assert response["record"] is not None and response["availability"] == "stale"
