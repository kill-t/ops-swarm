"""真实候选草案的可信审批请求、阶段有效性和原子事故迁移。"""

import asyncio
import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import event, select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from opsswarm.control_plane.sse import IncidentEventStream
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentEventRow, IncidentRow
from opsswarm.investigation.budget.queries import CallBudgetQueries
from tests.api.test_candidate_plan_api import create as create_plan
from tests.api.test_candidate_plan_api import get as get_plan
from tests.api.test_candidate_plan_api import ready as ready_plan
from tests.api.test_human_wait_api import command, post
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.identity_helpers import auth_header, token

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def ready(c, *, kind="terminal_handoff", with_plan=True):
    from opsswarm.approval.candidate.store import CandidateApprovalStore

    await ready_plan(c, kind=kind)
    c.app.state.candidate_approval_store = CandidateApprovalStore(c.db, clock=c.clock)
    c.approval_base = c.plan_base + "/approval-request"
    c.plan_record = None
    if with_plan:
        response = await create_plan(c)
        assert response.status_code == 201, response.text
        c.plan_record = response.json()["plan"]["record"]
    c.approval_body = dict(
        action="request_approval",
        plan_content_sha256=c.plan_record["content_sha256"] if c.plan_record else "0" * 64,
        expected_wait_version=c.plan_body["expected_wait_version"],
        expected_incident_version=c.incident_version,
        acknowledge_unresolved_conditions=True,
        idempotency_key="candidate-approval-command-118",
        reason="已阅读草案前置条件与未解问题，请求独立可信审批",
    )


async def create(c, body=None, *, header=None):
    return await c.client.post(
        c.approval_base,
        json=c.approval_body if body is None else body,
        headers=header or auth_header("engineer"),
    )


async def get(c):
    return await c.client.get(c.approval_base, headers=auth_header("viewer"))


async def fact_counts(c):
    async with c.db() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in ("candidate_approval_requests", "candidate_approval_commands")
            ]
        )


async def incident_events(c):
    async with c.db() as session:
        return list(
            await session.scalars(
                select(IncidentEventRow)
                .where(IncidentEventRow.incident_id == c.started.run.incident_id)
                .order_by(IncidentEventRow.id)
            )
        )


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_atomically_bind_request_and_event_without_changing_budget(
    wait_context, kind
):
    c = wait_context
    await ready(c, kind=kind)
    budget = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    run = await c.store.get_run(c.started.run.id)
    work = await c.store.get_work_item(c.started.work_item.id)
    attempts = await c.store.list_attempts(c.started.run.id)
    before_events = await incident_events(c)
    assert (await get(c)).json() == {"record": None, "availability": "pending"}
    response = await create(c)
    assert response.status_code == 201, response.text
    assert response.json()["applied"] is True
    view = response.json()["request"]
    record = view["record"]
    assert view["availability"] == "current" and record["status"] == "pending"
    assert record["id"] == str(uuid5(UUID(c.plan_record["id"]), "candidate-approval-request:v1"))
    assert record["plan_id"] == c.plan_record["id"]
    assert record["plan_content_sha256"] == c.plan_record["content_sha256"]
    assert record["investigation_run_id"] == str(c.started.run.id)
    assert record["candidate_job_id"] == str(c.job_id)
    assert record["wait_version"] == c.plan_body["expected_wait_version"]
    assert record["previous_incident_version"] == c.incident_version
    assert record["incident_version"] == c.incident_version + 1
    assert record["requested_by_id"] == "trusted-engineer"
    assert record["authorization_id"]
    assert datetime.fromisoformat(record["expires_at"]) == min(
        c.clock() + timedelta(minutes=30), budget.deadline
    )
    assert (await get(c)).json() == view
    replay = await create(c)
    assert replay.status_code == 200 and replay.json() == {"applied": False, "request": view}
    # 草案保持原内容；审批阶段按迁移后的事故版本读取，避免自我失效。
    plan = (await get_plan(c)).json()
    assert plan["record"] == c.plan_record and plan["availability"] == "stale"
    assert c.plan_record["content"]["plan"]["status"] == "draft"
    assert c.plan_record["content"]["approval_eligible"] is False
    assert c.plan_record["content"]["execution_eligible"] is False
    assert await CallBudgetQueries(c.db).get_snapshot(c.started.run.id) == budget
    assert await c.store.get_run(c.started.run.id) == run
    assert await c.store.get_work_item(c.started.work_item.id) == work
    assert await c.store.list_attempts(c.started.run.id) == attempts
    after_events = await incident_events(c)
    assert len(after_events) == len(before_events) + 1
    mutation = after_events[-1]
    assert mutation.id == record["event_cursor"]
    assert (mutation.from_status, mutation.to_status) == ("investigating", "awaiting_approval")
    assert mutation.incident_version == record["incident_version"]
    assert mutation.actor_id == record["requested_by_id"]
    assert mutation.reason == c.approval_body["reason"]
    assert mutation.occurred_at == datetime.fromisoformat(record["created_at"])
    assert mutation.payload == {"action": "request_approval"}
    events = await c.client.get(
        f"/api/v1/incidents/{run.incident_id}/events?after_cursor={before_events[-1].id}",
        headers=auth_header("viewer"),
    )
    assert events.status_code == 200 and len(events.json()["items"]) == 1
    assert events.json()["items"][0]["cursor"] == record["event_cursor"]

    async def connected():
        return False

    stream = IncidentEventStream(c.app.state.incident_store).stream(
        run.incident_id, after_cursor=before_events[-1].id, is_disconnected=connected
    )
    try:
        frame = await anext(stream)
        assert f"id: {record['event_cursor']}\n" in frame
        assert "event: incident.status_changed\n" in frame
        data = next(
            line.removeprefix("data: ") for line in frame.splitlines() if line.startswith("data: ")
        )
        assert json.loads(data) == events.json()["items"][0]
    finally:
        await stream.aclose()
    assert await fact_counts(c) == (1, 2)
    async with c.db() as session:
        incident = await session.get(IncidentRow, run.incident_id)
        assert incident.status == "awaiting_approval" and incident.version == c.incident_version + 1
        for table in ("action_plans", "action_steps", "approval_requests", "operation_executions"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    old_request = await c.client.get(
        f"/api/v1/approval-requests/{record['id']}", headers=auth_header("viewer")
    )
    assert old_request.status_code == 404
    old_decision = await c.client.post(
        f"/api/v1/approval-requests/{record['id']}/actions/approve",
        json={"expected_incident_version": record["incident_version"], "reason": "拒绝借用旧审批"},
        headers=auth_header("approver"),
    )
    assert old_decision.status_code == 404, old_decision.text
    old_plan = await c.client.post(
        f"/api/v1/action-plans/{record['plan_id']}/approval-requests",
        json={"expected_incident_version": record["incident_version"], "reason": "拒绝旧计划路径"},
        headers=auth_header("engineer"),
    )
    assert old_plan.status_code == 404, old_plan.text


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_concurrent_exact_replay_full_command_and_subject_conflicts_are_audited(wait_context):
    c = wait_context
    await ready(c)
    responses = await asyncio.gather(create(c), create(c))
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert responses[0].json()["request"] == responses[1].json()["request"]
    for change in (
        {"reason": "同幂等键下修改确认原因"},
        {"plan_content_sha256": "a" * 64},
        {"expected_wait_version": 2},
        {"expected_incident_version": c.incident_version + 1},
        {"idempotency_key": "another-candidate-approval-118"},
    ):
        refused = await create(c, {**c.approval_body, **change})
        assert refused.status_code == 409, refused.text
    other_engineer = {"Authorization": f"Bearer engineer-two.{token('engineer').split('.')[1]}"}
    assert (await create(c, header=other_engineer)).status_code == 409
    alternate = {key: value for key, value in c.approval_body.items() if key != "action"}
    for field in ("reason", "idempotency_key", "plan_content_sha256"):
        alternate[field] = " \n\t\u3000" + alternate[field] + " \n\t\u3000"
    replay = await c.client.post(
        c.approval_base,
        content=json.dumps(alternate, indent=2),
        headers={**auth_header("engineer"), "Content-Type": "application/json"},
    )
    assert replay.status_code == 200, replay.text
    page = await c.client.get(c.approval_base + "/commands?limit=2", headers=auth_header("viewer"))
    assert page.status_code == 200 and page.json()["next_cursor"] is not None
    items = page.json()["items"]
    rest = await c.client.get(
        c.approval_base + f"/commands?after_cursor={page.json()['next_cursor']}",
        headers=auth_header("viewer"),
    )
    assert rest.status_code == 200
    items += rest.json()["items"]
    assert [item["outcome"] for item in items].count("applied") == 1
    assert [item["outcome"] for item in items].count("replayed") == 2
    assert [item["outcome"] for item in items].count("error") == 6
    assert "raw_body" not in page.text + rest.text
    assert await fact_counts(c) == (1, 9)
    assert (
        len([item for item in await incident_events(c) if item.to_status == "awaiting_approval"])
        == 1
    )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_concurrent_different_commands_have_one_request_and_one_incident_transition(
    wait_context,
):
    c = wait_context
    await ready(c)
    before_events = await incident_events(c)
    responses = await asyncio.gather(
        create(c),
        create(c, {**c.approval_body, "idempotency_key": "competing-approval-command-118"}),
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    assert await fact_counts(c) == (1, 2)
    assert len(await incident_events(c)) == len(before_events) + 1
    assert (await get(c)).json()["availability"] == "current"


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_permissions_strict_body_and_missing_versions_do_not_create_facts(wait_context):
    c = wait_context
    await ready(c)
    for path in (c.approval_base, c.approval_base + "/commands"):
        assert (await c.client.get(path)).status_code == 401
        assert (await c.client.get(path, headers=auth_header("alert_source"))).status_code == 403
    assert (await c.client.post(c.approval_base, json=c.approval_body)).status_code == 401
    for role in ("viewer", "approver", "executor", "alert_source"):
        assert (await create(c, header=auth_header(role))).status_code == 403
    for change in (
        {"actor_id": "forged-engineer"},
        {"authorization_id": str(uuid4())},
        {"steps": []},
        {"expires_at": c.clock().isoformat()},
        {"expected_incident_version": True},
        {"expected_incident_version": "2"},
        {"expected_wait_version": True},
        {"acknowledge_unresolved_conditions": False},
        {"acknowledge_unresolved_conditions": 1},
        {"acknowledge_unresolved_conditions": "true"},
        {"reason": "\n \t\u3000"},
        {"reason": "长" * 501},
        {"plan_content_sha256": "not-a-hash"},
    ):
        response = await create(c, {**c.approval_body, **change})
        assert response.status_code == 422, response.text
    for field in ("expected_wait_version", "acknowledge_unresolved_conditions"):
        body = dict(c.approval_body)
        body.pop(field)
        assert (await create(c, body)).status_code == 422
    large = await c.client.post(
        c.approval_base,
        content=json.dumps(c.approval_body) + " " * 65536,
        headers={**auth_header("engineer"), "Content-Type": "application/json"},
    )
    assert large.status_code == 413
    assert await fact_counts(c) == (0, 0)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_hash_wait_and_incident_conflicts_are_audited_before_valid_admission(wait_context):
    c = wait_context
    await ready(c)
    for index, change in enumerate(
        (
            {"plan_content_sha256": "f" * 64},
            {"expected_wait_version": 2},
            {"expected_incident_version": c.incident_version + 1},
        )
    ):
        result = await create(
            c, {**c.approval_body, "idempotency_key": f"invalid-approval-{index}", **change}
        )
        assert result.status_code == 409, result.text
    assert await fact_counts(c) == (0, 3)
    missing = await c.client.post(
        c.approval_base.replace(str(c.job_id), str(uuid4())),
        json=c.approval_body,
        headers=auth_header("engineer"),
    )
    assert missing.status_code == 404, missing.text
    assert (await create(c)).status_code == 201


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_missing_plan_has_no_approval_and_new_plan_can_then_be_admitted(wait_context):
    c = wait_context
    await ready(c, with_plan=False)
    response = await create(c)
    assert response.status_code in (404, 409), response.text
    assert await fact_counts(c) == (0, 1)
    assert (await get(c)).status_code == 404
    result = await create_plan(c)
    assert result.status_code == 201, result.text
    c.approval_body["plan_content_sha256"] = result.json()["plan"]["record"]["content_sha256"]
    assert (await create(c)).status_code == 201


@pytest.mark.parametrize("wait_context", ["human_clean_with_change"], indirect=True)
@pytest.mark.parametrize("action", ["supplement", "cancel"])
async def test_human_input_change_preserves_stale_request_and_exact_replay(wait_context, action):
    c = wait_context
    await ready(c, kind="human_route")
    if action == "supplement":
        # 只有 awaiting_approval 快照不能开放输入，必须存在同运行的候选审批事实。
        async with c.db() as session, session.begin():
            await session.execute(
                text("UPDATE incidents SET status='awaiting_approval' WHERE id=:id"),
                {"id": c.started.run.incident_id},
            )
        refused = await post(c, command(c, version=2, key="unbound-approval-source-118"))
        assert refused.status_code == 409, refused.text
        async with c.db() as session, session.begin():
            await session.execute(
                text("UPDATE incidents SET status='investigating' WHERE id=:id"),
                {"id": c.started.run.incident_id},
            )
    accepted = await create(c)
    assert accepted.status_code == 201, accepted.text
    record = accepted.json()["request"]["record"]
    response = await post(c, command(c, action=action, version=2, key="approval-source-change-118"))
    assert response.status_code == 201, response.text
    if action == "supplement":
        following = await post(c, command(c, version=3, key="approval-source-following-118"))
        assert following.status_code == 201, following.text
    view = (await get(c)).json()
    assert view == {"record": record, "availability": "stale"}
    replay = await create(c)
    assert replay.status_code == 200 and replay.json() == {"applied": False, "request": view}
    refused = await create(c, {**c.approval_body, "idempotency_key": "new-after-input-118"})
    assert refused.status_code == 409, refused.text
    assert await fact_counts(c) == (1, 3)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("change", ["incident", "deadline"])
async def test_history_currentness_reacts_to_incident_change_or_deadline_without_writes(
    wait_context, change
):
    c = wait_context
    await ready(c)
    response = await create(c)
    assert response.status_code == 201, response.text
    record = response.json()["request"]["record"]
    if change == "incident":
        async with c.db() as session, session.begin():
            await session.execute(
                text("UPDATE incidents SET version=version+1 WHERE id=:id"),
                {"id": c.started.run.incident_id},
            )
    else:
        c.clock.current = datetime.fromisoformat(record["expires_at"])
    counts = await fact_counts(c)
    expected = {"record": record, "availability": "stale" if change == "incident" else "expired"}
    assert (await get(c)).json() == expected
    assert await fact_counts(c) == counts
    replay = await create(c)
    assert replay.status_code == 200 and replay.json() == {"applied": False, "request": expected}
    assert (
        await create(c, {**c.approval_body, "idempotency_key": "new-after-stale-118"})
    ).status_code == 409
    async with c.db() as session:
        incident = await session.get(IncidentRow, c.started.run.incident_id)
        assert incident.status == "awaiting_approval"


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("phase", ["before", "after_flush", "auth_after_flush"])
async def test_deadline_and_auth_recheck_roll_back_request_incident_and_event(wait_context, phase):
    c = wait_context
    await ready(c)
    before_events = await incident_events(c)
    before_budget = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    if phase == "before":
        c.clock.current += timedelta(seconds=901)
    advanced = False

    def expire(session, flush_context):
        nonlocal advanced
        if (
            phase != "before"
            and not advanced
            and any(row.__tablename__ == "candidate_approval_commands" for row in session.new)
        ):
            advanced = True
            c.clock.current += (
                timedelta(days=2) if phase == "auth_after_flush" else timedelta(seconds=901)
            )

    event.listen(Session, "after_flush", expire)
    try:
        response = await create(c)
    finally:
        event.remove(Session, "after_flush", expire)
    assert response.status_code == (401 if phase == "auth_after_flush" else 409), response.text
    assert await fact_counts(c) == (0, 1 if phase == "before" else 0)
    assert len(await incident_events(c)) == len(before_events)
    assert await CallBudgetQueries(c.db).get_snapshot(c.started.run.id) == before_budget
    async with c.db() as session:
        incident = await session.get(IncidentRow, c.started.run.incident_id)
        assert incident.status == "investigating" and incident.version == c.incident_version


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_auth_expired_after_authorization_lock_rejects_before_business_writes(
    wait_context, monkeypatch
):
    c = wait_context
    await ready(c)
    original = AsyncSession.get
    advanced = False

    async def acquire(self, entity, *args, **kwargs):
        nonlocal advanced
        row = await original(self, entity, *args, **kwargs)
        if entity is OperatorAuthorizationRow and kwargs.get("with_for_update") and not advanced:
            advanced = True
            c.clock.current += timedelta(days=2)
        return row

    monkeypatch.setattr(AsyncSession, "get", acquire)
    response = await create(c)
    assert advanced and response.status_code == 401, response.text
    assert response.json()["error_code"] == "operator_unauthenticated"
    assert await fact_counts(c) == (0, 0)
