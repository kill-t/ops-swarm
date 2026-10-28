"""真实132来源的任务分派、幂等并发、历史及原预算隔离。"""

import asyncio
import json

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_dispatch.queries import DispatchQueries
from opsswarm.investigation.reinvestigation_dispatch.store import DispatchStore
from tests.api import test_reinvestigation_coordination_api as coordination
from tests.api.test_reinvestigation_coordination_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_reinvestigation_coordination_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_coordination_api import wait_context as wait_context
from tests.identity_helpers import auth_header

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def prepare(c, kind="terminal_handoff", model=None, *, reuse_success=False):
    try:
        await coordination.prepare(c, kind)
    except AssertionError as error:
        if hasattr(c, "convergence"):
            source = await coordination.admission.convergence.get(c)
            raise AssertionError(f"133之前的失败收敛来源未就绪：{source.text}") from error
        raise
    c.model = model or coordination.FakeModel()
    original_complete = c.reflow_store.complete
    if reuse_success:

        async def crash(*args):
            raise RuntimeError("模拟成功调用结算后保存中断")

        c.reflow_store.complete = crash
    assert await coordination.worker(c, c.model).run_once()
    if reuse_success:
        c.reflow_store.complete = original_complete
        await asyncio.sleep(2)
        assert await coordination.worker(c, c.model).run_once()
        assert len(c.model.calls) == 1
    response = await coordination.get(c)
    assert response.status_code == 200 and response.json()["status"] == "succeeded", response.text
    c.dispatch_store = DispatchStore(c.db, clock=c.clock)
    c.dispatch_queries = DispatchQueries(c.db, clock=c.clock)
    c.app.state.dispatch_queries = c.dispatch_queries
    c.dispatch_base = f"/api/v1/investigations/{c.started.run.id}/reinvestigation-dispatch"


async def get(c, headers=None):
    return await c.client.get(
        c.dispatch_base, headers=auth_header("viewer") if headers is None else headers
    )


async def counts(c):
    async with c.db() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {t}"))
                for t in ("investigation_reflow_dispatches", "investigation_reflow_task_queue")
            ]
        )


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [
        ("candidate_with_change", "terminal_handoff"),
        ("human_clean_with_change", "human_route"),
    ],
    indirect=["wait_context"],
)
async def test_two_sources_create_exact_pending_tasks_once_without_changing_history(
    wait_context, kind
):
    c = wait_context
    await prepare(c, kind)
    before, calls = await coordination.root_facts(c), await coordination.calls(c)
    original = (await coordination.get(c)).json()
    assert (await get(c)).json()["availability"] == "pending"
    assert (await get(c, {})).status_code == 401
    assert (await get(c, auth_header("alert_source"))).status_code == 403
    c.app.state.operator_auth_settings = c.settings.model_copy(update={"mode": "legacy_local"})
    try:
        assert (await get(c, {})).status_code == 401
        assert (await get(c)).status_code == 401
    finally:
        c.app.state.operator_auth_settings = c.settings
    results = await asyncio.gather(
        c.dispatch_store.dispatch_next(), c.dispatch_store.dispatch_next()
    )
    assert sorted(results) == [False, True]
    response = await get(c)
    assert response.status_code == 200, response.text
    body = response.json()
    assert response.headers["cache-control"] == "no-store"
    assert body["record"]["status"] == "queued" and body["availability"] == "current"
    assert len(body["tasks"]) == len(original["result"]["proposal"]["tasks"])
    assert (
        body["record"]["assignment"]["round_number"]
        == original["result"]["proposal"]["round_number"]
    )
    for task in body["tasks"]:
        assert task["status"] == "pending"
        assert task["assignment"]["evidence_snapshot_id"] is None
        assert task["assignment"]["window_start"] >= body["record"]["observation_floor_at"]
        assert task["assignment"]["window_start"] < task["assignment"]["window_end"]
    assert await coordination.root_facts(c) == before
    assert await coordination.calls(c) == calls
    assert (await coordination.get(c)).json() == original
    assert not await c.dispatch_store.dispatch_next()
    assert (await get(c)).json() == body
    # 后续告警改变当前适用性，132和133的冻结历史仍能完整读取。
    await coordination.admission.attach_old_alert(c)
    stale = await get(c)
    assert stale.status_code == 200 and stale.json()["availability"] == "stale", stale.text
    assert stale.json()["record"] == body["record"] and stale.json()["tasks"] == body["tasks"]
    assert (await coordination.get(c)).json() == original


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_new_alert_before_dispatch_saves_blocked_without_tasks(wait_context):
    c = wait_context
    await prepare(c)
    before = await coordination.calls(c)
    await coordination.admission.attach_old_alert(c)
    assert await c.dispatch_store.dispatch_next()
    response = await get(c)
    assert response.status_code == 200, response.text
    body = response.json()
    assert (
        body["record"]["status"] == "blocked"
        and body["record"]["reason_code"] == "reflow_source_changed"
    )
    assert body["tasks"] == [] and body["record"]["assignment"] is None
    assert await counts(c) == (1, 0)
    assert await coordination.calls(c) == before and not await c.dispatch_store.dispatch_next()


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_transaction_failure_rolls_back_whole_queue_and_damage_is_rejected(wait_context):
    c = wait_context
    await prepare(c, reuse_success=True)
    before, calls = await coordination.root_facts(c), await coordination.calls(c)

    def crash(session, flush_context):
        if any(
            getattr(row, "__tablename__", "") == "investigation_reflow_task_queue"
            for row in session.new
        ):
            raise RuntimeError("模拟整套任务写后提交前崩溃")

    event.listen(Session, "after_flush", crash)
    try:
        with pytest.raises(RuntimeError, match="提交前崩溃"):
            await c.dispatch_store.dispatch_next()
    finally:
        event.remove(Session, "after_flush", crash)
    assert await counts(c) == (0, 0)
    async with c.db() as session:
        deadline = await session.scalar(
            text(
                "SELECT started_at+make_interval(secs => "
                "(call_budget->>'max_duration_seconds')::integer) "
                "FROM investigation_runs WHERE id=:id"
            ),
            {"id": c.started.run.id},
        )

    def expire_after_flush(session, flush_context):
        if any(
            getattr(row, "__tablename__", "") == "investigation_reflow_task_queue"
            for row in session.new
        ):
            c.clock.live = False
            c.clock.current = deadline

    event.listen(Session, "after_flush", expire_after_flush)
    try:
        with pytest.raises(ReflowError) as expired:
            await c.dispatch_store.dispatch_next()
        assert expired.value.code == "run_deadline_exceeded"
    finally:
        event.remove(Session, "after_flush", expire_after_flush)
        c.clock.live = True
    assert await counts(c) == (0, 0)
    assert await c.dispatch_store.dispatch_next()
    body = (await get(c)).json()
    identifier = body["tasks"][0]["id"]
    assignment = body["tasks"][0]["assignment"]

    sql = "UPDATE investigation_reflow_task_queue SET assignment=CAST(:body AS jsonb) WHERE id=:id"
    try:
        damaged = {**assignment, "service_name": "another-service"}
        await coordination.admission.replace(
            c, sql, {"body": json.dumps(damaged), "id": identifier}
        )
        response = await get(c)
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == "dispatch_integrity_violation"
    finally:
        await coordination.admission.replace(
            c, sql, {"body": json.dumps(assignment), "id": identifier}
        )
    assert (await get(c)).json() == body
    assert await coordination.root_facts(c) == before and await coordination.calls(c) == calls
