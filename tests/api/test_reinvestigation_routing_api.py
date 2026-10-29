"""完整隔离链上的确定性路由、原生编译与历史保护。"""

import asyncio
import copy
import json
from datetime import timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.investigation.reinvestigation_routing.queries import ReflowRoutingQueries
from opsswarm.investigation.reinvestigation_routing.store import ReflowRoutingStore
from tests.api import test_reinvestigation_review_api as review
from tests.api.test_reinvestigation_review_api import current_fixture_time as current_fixture_time
from tests.api.test_reinvestigation_review_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_review_api import wait_context as wait_context
from tests.identity_helpers import auth_header
from tests.integration.test_candidate_approval_migration import config

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def setup(c, kind="terminal_handoff", direction="evaluate_for_routing", *, reuse=False):
    await review.setup(c, kind)
    c.routing_store = ReflowRoutingStore(c.db, clock=c.clock)
    c.routing_queries = ReflowRoutingQueries(c.db, clock=c.clock)
    c.app.state.reflow_routing_queries = c.routing_queries
    c.routing_base = f"/api/v1/investigations/{c.started.run.id}/reinvestigation-routing"
    assert not await c.routing_store.record_next()
    model = review.Model(direction=direction)
    original = c.review_store.complete
    if reuse:

        async def crash(*args):
            raise RuntimeError("模拟136成功后未发布")

        c.review_store.complete = crash
    assert await review.worker(c, model).run_once()
    if reuse:
        c.review_store.complete = original
        await asyncio.sleep(2)
        assert await review.worker(c, model).run_once()
        assert len(model.calls) == 1
    response = await review.get(c)
    assert response.status_code == 200 and response.json()["status"] == "succeeded", (
        response.text,
        c.review_errors,
    )


async def get(c, headers=None):
    return await c.client.get(
        c.routing_base, headers=auth_header("viewer") if headers is None else headers
    )


async def native_content(c, session, now=None):
    return await session.scalar(
        text(
            "SELECT reflow_routing_content(:id,:at,(SELECT max(e.id) "
            "FROM incident_events e WHERE incident_id=:incident))"
        ),
        {"id": c.started.run.id, "at": now or c.clock(), "incident": c.started.run.incident_id},
    )


async def insert(session, content):
    await session.execute(
        text(
            "INSERT INTO investigation_reflow_routes"
            "(investigation_run_id,route,content,content_sha256,created_at) "
            "VALUES(:id,:route,CAST(:content AS jsonb),:hash,:at)"
        ),
        {
            "id": content["investigation_run_id"],
            "route": content["route"],
            "content": json.dumps(content, ensure_ascii=False),
            "hash": raw_content_hash(content),
            "at": content["created_at"],
        },
    )


@pytest.mark.parametrize(
    "wait_context,kind,direction,expected",
    [
        (
            "candidate_with_change",
            "terminal_handoff",
            "evaluate_for_routing",
            "candidate_evaluation",
        ),
        ("human_clean_with_change", "human_route", "follow_up", "follow_up"),
    ],
    indirect=["wait_context"],
)
async def test_two_sources_concurrent_route_full_binding_and_native_guards(
    wait_context, kind, direction, expected
):
    c = wait_context
    await setup(c, kind, direction, reuse=kind == "human_route")
    before_review = (await review.get(c)).json()
    calls = await review.synthesis.execution.dispatch.coordination.calls(c)
    root = await review.synthesis.execution.dispatch.coordination.root_facts(c)
    assert (await get(c, {})).status_code == 401
    assert (await get(c, auth_header("alert_source"))).status_code == 403
    assert (await get(c)).json()["availability"] == "pending"
    async with c.db() as session, session.begin():
        native = await native_content(c, session)
        assert native["route"] == expected
        for field, value in [
            ("review_result_sha256", "0" * 64),
            ("dispatch_status", "dispatched"),
            ("unresolved_questions", []),
            ("minimum_model_calls", 0),
            ("source_call_id", "00000000-0000-0000-0000-000000000000"),
        ]:
            bad = copy.deepcopy(native)
            bad[field] = value
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await insert(session, bad)
        bad = copy.deepcopy(native)
        bad["base_decision"]["existing_task_count"] -= 1
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await insert(session, bad)
        bad = copy.deepcopy(native)
        bad["budget"]["calls"] = bad["budget"]["calls"][:-1]
        with pytest.raises(DBAPIError):
            async with session.begin_nested():
                await insert(session, bad)
    assert sorted(
        await asyncio.gather(c.routing_store.record_next(), c.routing_store.record_next())
    ) == [False, True]
    response = await get(c)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    record = body["record"]
    assert record["route"] == expected and record["dispatch_status"] == "not_dispatched"
    assert not await c.routing_store.record_next()
    assert calls == await review.synthesis.execution.dispatch.coordination.calls(c)
    assert root == await review.synthesis.execution.dispatch.coordination.root_facts(c)
    assert before_review == (await review.get(c)).json()
    async with c.db() as session, session.begin():
        native = await session.scalar(
            text("SELECT reflow_routing_content(:id,:at,:cursor)"),
            {
                "id": c.started.run.id,
                "at": record["created_at"],
                "cursor": record["basis_event_cursor"],
            },
        )
        assert native == record
        assert record["budget"]["model_usage"]["unknown_model_calls"] > 0
        for sql in (
            "UPDATE investigation_reflow_routes SET content=content",
            "DELETE FROM investigation_reflow_routes",
        ):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await session.execute(text(sql))
    with pytest.raises(DBAPIError):
        await asyncio.to_thread(command.downgrade, config(), "0091_reflow_review")
    # 迟到的旧接收时间告警仍使当前诊断失效，历史正文保持。
    await review.synthesis.execution.dispatch.coordination.admission.attach_old_alert(c)
    stale = await get(c)
    assert stale.status_code == 200, stale.text
    assert stale.json()["record"] == record
    assert stale.json()["availability"] == "stale"
    c.routing_queries._clock = lambda: c.clock() + timedelta(seconds=1000)
    assert (await get(c)).json()["record"] == record


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_commit_rechecks_new_event_and_stale_source_records_human(wait_context):
    c = wait_context
    await setup(c)
    wrote_event = False
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            content = await native_content(c, session)
            assert content["route"] == "candidate_evaluation"
            await insert(session, content)
            await session.execute(
                text(
                    "INSERT INTO incident_events(incident_id,event_type,actor_type,to_status,"
                    "reason,incident_version,payload,occurred_at) SELECT id,"
                    "'incident.alert_attached','system',status,'模拟提交前新告警',version,"
                    "'{}'::jsonb,clock_timestamp() FROM incidents WHERE id=:id"
                ),
                {"id": c.started.run.incident_id},
            )
            wrote_event = True
    assert wrote_event, "插入和追加事件均成功，错误必须由事务提交门禁产生"
    assert (await get(c)).json()["record"] is None
    await review.synthesis.execution.dispatch.coordination.admission.attach_old_alert(c)
    assert await c.routing_store.record_next()
    response = await get(c)
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record["route"] == "await_human"
    assert record["basis_blocker"] == "reflow_source_changed"
    assert record["blocking_reason_codes"][0] == "reflow_source_changed"
