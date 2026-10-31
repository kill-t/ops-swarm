"""同一回流草案上的可信审批请求、阶段迁移、原子性与历史。"""

import asyncio
import json
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from opsswarm.approval.reinvestigation_candidate.store import ReflowApprovalStore
from opsswarm.approval.reinvestigation_candidate.tables import ReflowApprovalRequestRow
from opsswarm.control_plane.sse import IncidentEventStream
from opsswarm.investigation.budget.queries import CallBudgetQueries
from tests.api import test_reinvestigation_candidate_plan_api as plan
from tests.api.test_candidate_approval_api import incident_events
from tests.api.test_reinvestigation_candidate_plan_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_reinvestigation_candidate_plan_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_candidate_plan_api import wait_context as wait_context
from tests.identity_helpers import auth_header
from tests.integration.test_candidate_approval_migration import config

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def ready(c, monkeypatch):
    await plan.ready(c, monkeypatch)
    created = await plan.post(c)
    assert created.status_code == 201, created.text
    c.reflow_plan_record = created.json()["plan"]["record"]
    c.app.state.reflow_approval_store = ReflowApprovalStore(c.db, clock=c.clock)
    c.reflow_approval_base = c.reflow_plan_base + "/approval-request"
    c.reflow_approval_body = {
        "action": "request_approval",
        "plan_content_sha256": c.reflow_plan_record["content_sha256"],
        "expected_incident_version": c.reflow_plan_record["incident_version"],
        "acknowledge_unresolved_conditions": True,
        "idempotency_key": "reflow-approval-command-140",
        "reason": "已阅读本次新草案的前置条件与未解问题，请求独立可信审批",
    }


async def post(c, body=None, headers=None):
    return await c.client.post(
        c.reflow_approval_base,
        json=c.reflow_approval_body if body is None else body,
        headers=auth_header("engineer") if headers is None else headers,
    )


async def get(c, suffix="", headers=None):
    return await c.client.get(
        c.reflow_approval_base + suffix,
        headers=auth_header("viewer") if headers is None else headers,
    )


async def request_count(c):
    async with c.db() as session:
        return await session.scalar(text("SELECT count(*) FROM reinvestigation_approval_requests"))


async def incident_fact(c):
    async with c.db() as session:
        return (
            await session.execute(
                text("SELECT status,version FROM incidents WHERE id=:id"),
                {"id": c.started.run.incident_id},
            )
        ).one()


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_reflow_request_owns_atomic_approval_phase_and_preserves_original_facts(
    wait_context,
    monkeypatch,
):
    c = wait_context
    await ready(c, monkeypatch)
    execution = plan.candidate.routing.review.synthesis.execution
    old_root = await execution.dispatch.coordination.root_facts(c)
    old_budget = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    old_authorizations = await plan.candidate.authorization_counts(c)
    old_events = await incident_events(c)
    before_incident = await incident_fact(c)
    assert before_incident.status == "investigating"
    assert (await get(c)).json() == {"record": None, "availability": "pending"}
    assert (await post(c, headers={})).status_code == 401
    for role in ("viewer", "approver", "executor", "alert_source"):
        assert (await post(c, headers=auth_header(role))).status_code == 403
    for suffix in ("", "/commands"):
        assert (await get(c, suffix, headers={})).status_code == 401
        assert (await get(c, suffix, headers=auth_header("alert_source"))).status_code == 403
    for change in (
        {"acknowledge_unresolved_conditions": False},
        {"acknowledge_unresolved_conditions": 1},
        {"expected_wait_version": None},
        {"expires_at": "2099-01-01T00:00:00Z"},
    ):
        refused = await post(c, c.reflow_approval_body | change)
        assert refused.status_code == 422, refused.text
    for index, change in enumerate(
        (
            {"plan_content_sha256": "0" * 64},
            {"expected_incident_version": before_incident.version + 1},
        )
    ):
        refused = await post(
            c,
            c.reflow_approval_body
            | change
            | {"idempotency_key": f"invalid-reflow-approval-{index}"},
        )
        assert refused.status_code == 409, refused.text
    assert await request_count(c) == 0

    wrote_transition = False
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE incidents SET status='awaiting_approval',version=version+1 WHERE id=:id"
                ),
                {"id": c.started.run.incident_id},
            )
            wrote_transition = True
    assert wrote_transition, "孤立事故迁移应在提交时因缺少请求、事件和成功审计而拒绝"
    assert await incident_fact(c) == before_incident

    injected = False

    def attach_alert_after_request(session, flush_context):
        nonlocal injected
        if not injected and any(isinstance(row, ReflowApprovalRequestRow) for row in session.new):
            session.execute(
                text(
                    "INSERT INTO incident_events(incident_id,event_type,actor_type,to_status,"
                    "reason,incident_version,payload,occurred_at) SELECT id,"
                    "'incident.alert_attached','system',status,'模拟审批请求提交前新告警',version,"
                    "'{}'::jsonb,clock_timestamp() FROM incidents WHERE id=:id"
                ),
                {"id": c.started.run.incident_id},
            )
            injected = True

    event.listen(Session, "after_flush", attach_alert_after_request)
    try:
        try:
            refused = await post(c)
        except DBAPIError:
            pass
        else:
            assert refused.status_code == 409, refused.text
    finally:
        event.remove(Session, "after_flush", attach_alert_after_request)
    assert injected, "必须实际写入请求并追加告警，不能把前置失败误认为提交守卫"
    assert await request_count(c) == 0
    assert await incident_fact(c) == before_incident
    assert len(await incident_events(c)) == len(old_events)

    responses = await asyncio.gather(post(c), post(c))
    assert sorted(response.status_code for response in responses) == [200, 201], [
        r.text for r in responses
    ]
    created = next(response for response in responses if response.status_code == 201)
    assert created.headers["cache-control"] == "no-store"
    view = created.json()["request"]
    record = view["record"]
    assert view["availability"] == "current" and record["status"] == "pending"
    assert record["investigation_run_id"] == record["candidate_job_id"] == str(c.started.run.id)
    assert record["plan_id"] == c.reflow_plan_record["id"]
    assert record["plan_content_sha256"] == c.reflow_plan_record["content_sha256"]
    assert record["basis_event_cursor"] == old_events[-1].id
    assert record["previous_incident_version"] == before_incident.version
    assert record["incident_version"] == before_incident.version + 1
    assert record["requested_by_id"] == "trusted-engineer"
    assert datetime.fromisoformat(record["expires_at"]) == min(
        datetime.fromisoformat(record["created_at"]) + timedelta(minutes=30), old_budget.deadline
    )
    current = await get(c)
    assert current.json() == view and current.headers["cache-control"] == "no-store"
    assert (await post(c)).status_code == 200
    old_plan = await plan.get(c)
    assert old_plan.json() == {"record": c.reflow_plan_record, "availability": "stale"}
    assert c.reflow_plan_record["content"]["approval_eligible"] is False
    assert c.reflow_plan_record["content"]["execution_eligible"] is False
    assert await CallBudgetQueries(c.db).get_snapshot(c.started.run.id) == old_budget
    assert old_root == await execution.dispatch.coordination.root_facts(c)
    assert old_authorizations == await plan.candidate.authorization_counts(c)
    events = await incident_events(c)
    assert len(events) == len(old_events) + 1
    mutation = events[-1]
    assert mutation.id == record["event_cursor"]
    assert (mutation.from_status, mutation.to_status) == ("investigating", "awaiting_approval")
    assert mutation.incident_version == record["incident_version"]
    assert mutation.actor_type == "human" and mutation.actor_id == record["requested_by_id"]
    assert mutation.reason == c.reflow_approval_body["reason"]
    assert mutation.payload == {"action": "request_approval"}
    assert mutation.occurred_at == datetime.fromisoformat(record["created_at"])
    replay_events = await c.client.get(
        f"/api/v1/incidents/{c.started.run.incident_id}/events?after_cursor={old_events[-1].id}",
        headers=auth_header("viewer"),
    )
    assert replay_events.status_code == 200 and len(replay_events.json()["items"]) == 1
    assert replay_events.json()["items"][0]["cursor"] == record["event_cursor"]

    async def connected():
        return False

    stream = IncidentEventStream(c.app.state.incident_store).stream(
        c.started.run.incident_id, after_cursor=old_events[-1].id, is_disconnected=connected
    )
    try:
        frame = await anext(stream)
        assert f"id: {record['event_cursor']}\n" in frame
        assert "event: incident.status_changed\n" in frame
        data = next(
            line.removeprefix("data: ") for line in frame.splitlines() if line.startswith("data: ")
        )
        assert json.loads(data) == replay_events.json()["items"][0]
    finally:
        await stream.aclose()
    for change in ({"reason": "另一确认原因"}, {"idempotency_key": "another-reflow-approval"}):
        assert (await post(c, c.reflow_approval_body | change)).status_code == 409
    audits = await get(c, "/commands?limit=2")
    assert audits.status_code == 200 and audits.headers["cache-control"] == "no-store"
    assert audits.json()["next_cursor"] is not None
    rest = await get(c, f"/commands?after_cursor={audits.json()['next_cursor']}")
    items = audits.json()["items"] + rest.json()["items"]
    outcomes = [item["outcome"] for item in items]
    assert outcomes.count("applied") == 1 and outcomes.count("replayed") == 2
    assert outcomes.count("error") == 4
    assert "raw_body" not in audits.text + rest.text
    assert await request_count(c) == 1

    for sql in (
        "UPDATE reinvestigation_approval_requests SET expires_at=expires_at",
        "DELETE FROM reinvestigation_approval_requests",
        "UPDATE reinvestigation_approval_commands SET command=command",
        "DELETE FROM reinvestigation_approval_commands",
    ):
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text(sql))
    for field, value in (
        ("plan_content_sha256", "0" * 64),
        ("basis_event_cursor", record["basis_event_cursor"] - 1),
        ("expires_at", datetime.fromisoformat(record["expires_at"]) - timedelta(seconds=1)),
    ):
        wrote_forged = False
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text("SET LOCAL session_replication_role='replica'"))
                await session.execute(
                    text(
                        f"UPDATE reinvestigation_approval_requests SET {field}=:value WHERE id=:id"
                    ),
                    {"value": value, "id": UUID(record["id"])},
                )
                wrote_forged = True
                await session.execute(text("SET LOCAL session_replication_role='origin'"))
                await session.execute(
                    text(
                        "SELECT assert_reflow_approval_request(r,false) "
                        "FROM reinvestigation_approval_requests r"
                    )
                )
        assert wrote_forged, "篡改值应先通过基础CHECK，由完整历史重建拒绝"
    async with c.db() as session:
        assert (
            await session.scalar(
                text(
                    "SELECT reflow_approval_basis_blocker(r,clock_timestamp()) "
                    "FROM reinvestigation_approval_requests r"
                )
            )
            is None
        )
    old_decision = await c.client.post(
        f"/api/v1/approval-requests/{record['id']}/actions/approve",
        json={"expected_incident_version": record["incident_version"], "reason": "拒绝旧批准路径"},
        headers=auth_header("approver"),
    )
    assert old_decision.status_code == 404, old_decision.text
    with pytest.raises(DBAPIError):
        await asyncio.to_thread(command.downgrade, config(), "0094_reflow_candidate_plan")
    await execution.dispatch.coordination.admission.attach_old_alert(c)
    stale = await get(c)
    assert stale.json() == {"record": record, "availability": "stale"}
    queries = c.app.state.reflow_approval_store
    old_clock = queries._clock
    queries._clock = lambda: old_budget.deadline + timedelta(seconds=1)
    try:
        expired = await get(c)
        assert expired.status_code == 200 and expired.json() == {
            "record": record,
            "availability": "expired",
        }
    finally:
        queries._clock = old_clock
    assert (await post(c)).status_code == 200
    assert (
        await post(c, c.reflow_approval_body | {"idempotency_key": "stale-reflow-approval"})
    ).status_code == 409
    assert (await get(c, "/commands")).status_code == 200
    assert await request_count(c) == 1
