"""真实候选审批决定、独立历史适用性与可信过期收敛。"""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from opsswarm.control_plane.sse import IncidentEventStream
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.candidate.tables import CandidateJobRow
from tests.api.test_candidate_approval_api import create as create_request
from tests.api.test_candidate_approval_api import get as get_request
from tests.api.test_candidate_approval_api import incident_events
from tests.api.test_candidate_approval_api import ready as ready_request
from tests.api.test_candidate_plan_api import get as get_plan
from tests.api.test_human_wait_api import command, post
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.identity_helpers import auth_header, token

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def ready(c, *, kind="terminal_handoff"):
    from opsswarm.approval.decision.store import CandidateDecisionStore

    await ready_request(c, kind=kind)
    c.app.state.candidate_decision_store = CandidateDecisionStore(c.db, clock=c.clock)
    c.decision_base = c.approval_base + "/decision"
    response = await create_request(c)
    assert response.status_code == 201, response.text
    c.request_record = response.json()["request"]["record"]
    c.decision_body = dict(
        action="approve",
        request_id=c.request_record["id"],
        plan_content_sha256=c.request_record["plan_content_sha256"],
        expected_incident_version=c.request_record["incident_version"],
        acknowledge_unresolved_conditions=True,
        idempotency_key="candidate-decision-command-119",
        reason="已阅读原前置条件及未解问题，记录可信人工判断",
    )


async def decide(c, body=None, *, header=None):
    return await c.client.post(
        c.decision_base,
        json=c.decision_body if body is None else body,
        headers=header or auth_header("approver"),
    )


async def get(c):
    return await c.client.get(c.decision_base, headers=auth_header("viewer"))


async def fact_counts(c):
    async with c.db() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in ("candidate_approval_decisions", "candidate_decision_commands")
            ]
        )


async def parent_facts(c):
    return (
        await CallBudgetQueries(c.db).get_snapshot(c.started.run.id),
        await c.store.get_run(c.started.run.id),
        await c.store.get_work_item(c.started.work_item.id),
        await c.store.list_attempts(c.started.run.id),
    )


def negative_command(c, action):
    return {
        **c.decision_body,
        "action": action,
        "acknowledge_unresolved_conditions": False,
    }


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_approve_atomically_without_self_invalidating_or_granting_execution(
    wait_context, kind
):
    c = wait_context
    await ready(c, kind=kind)
    before_parent = await parent_facts(c)
    before_events = await incident_events(c)
    assert (await get(c)).json() == {"record": None, "availability": "pending"}
    response = await decide(c)
    assert response.status_code == 201, response.text
    assert response.json()["applied"] is True
    view = response.json()["decision"]
    record, content = view["record"], view["record"]["content"]
    assert view["availability"] == "current" and record["outcome"] == "approved"
    assert record["id"] == str(
        uuid5(UUID(c.request_record["id"]), "candidate-approval-decision:v1")
    )
    assert record["request_id"] == c.request_record["id"]
    assert record["investigation_run_id"] == str(c.started.run.id)
    assert record["candidate_job_id"] == str(c.job_id)
    assert record["plan_content_sha256"] == c.plan_record["content_sha256"]
    assert record["previous_incident_version"] == c.request_record["incident_version"]
    assert record["incident_version"] == c.request_record["incident_version"] + 1
    assert record["decided_by_id"] == "trusted-approver" and record["authorization_id"]
    assert record["policy_version"] == "candidate-approval-decision-v1"
    assert content["version"] == "candidate-approval-decision-content-v1"
    assert content["request_id"] == c.request_record["id"]
    assert content["plan_id"] == c.plan_record["id"]
    assert content["plan_content_sha256"] == c.plan_record["content_sha256"]
    assert content["candidate_job_id"] == str(c.job_id)
    assert content["wait_version"] == c.request_record["wait_version"]
    assert content["outcome"] == "approved" and content["execution_eligible"] is False
    for name in ("evidence_ids", "preconditions", "unresolved_questions"):
        assert content[name] == c.plan_record["content"][name]
    assert (
        record["content_sha256"]
        == hashlib.sha256(
            json.dumps(
                content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        ).hexdigest()
    )
    assert (await get(c)).json() == view
    replay = await decide(c)
    assert replay.status_code == 200 and replay.json() == {"applied": False, "decision": view}
    assert (await get_request(c)).json() == {
        "record": c.request_record,
        "availability": "stale",
    }
    plan = (await get_plan(c)).json()
    assert plan["record"] == c.plan_record and plan["availability"] == "stale"
    assert c.plan_record["content"]["plan"]["status"] == "draft"
    assert c.plan_record["content"]["approval_eligible"] is False
    assert c.plan_record["content"]["execution_eligible"] is False
    assert await parent_facts(c) == before_parent
    after_events = await incident_events(c)
    assert len(after_events) == len(before_events) + 1
    mutation = after_events[-1]
    assert mutation.id == record["event_cursor"]
    assert (mutation.from_status, mutation.to_status) == ("awaiting_approval", "mitigating")
    assert mutation.incident_version == record["incident_version"]
    assert mutation.actor_id == record["decided_by_id"]
    assert mutation.reason == c.decision_body["reason"]
    assert mutation.occurred_at == datetime.fromisoformat(record["created_at"])
    assert mutation.payload == {"action": "approve_plan"}
    events = await c.client.get(
        f"/api/v1/incidents/{c.started.run.incident_id}/events?after_cursor={before_events[-1].id}",
        headers=auth_header("viewer"),
    )
    assert events.status_code == 200 and len(events.json()["items"]) == 1
    assert events.json()["items"][0]["cursor"] == record["event_cursor"]

    async def connected():
        return False

    stream = IncidentEventStream(c.app.state.incident_store).stream(
        c.started.run.incident_id, after_cursor=before_events[-1].id, is_disconnected=connected
    )
    try:
        frame = await anext(stream)
        assert f"id: {record['event_cursor']}\n" in frame
        assert "event: incident.status_changed\n" in frame
        payload = next(
            line.removeprefix("data: ") for line in frame.splitlines() if line.startswith("data: ")
        )
        assert json.loads(payload) == events.json()["items"][0]
    finally:
        await stream.aclose()
    assert await fact_counts(c) == (1, 2)
    async with c.db() as session:
        incident = await session.get(IncidentRow, c.started.run.incident_id)
        assert incident.status == "mitigating" and incident.version == record["incident_version"]
        job = await session.get(CandidateJobRow, c.job_id)
        assert content["source_content_sha256"] == job.source_content_sha256
        admission = await session.get(OperatorAuthorizationRow, UUID(record["authorization_id"]))
        assert admission.operation == "decide_candidate_approval"
        assert admission.subject_id == "trusted-approver" and admission.role == "approver"
        assert admission.resources == {"run_id": str(c.started.run.id), "job_id": str(c.job_id)}
        for table in ("action_plans", "action_steps", "approval_requests", "operation_executions"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    old = await c.client.post(
        f"/api/v1/approval-requests/{record['request_id']}/actions/approve",
        json={"expected_incident_version": record["incident_version"], "reason": "拒绝旧审批路径"},
        headers=auth_header("approver"),
    )
    assert old.status_code == 404, old.text
    execute = await c.client.post(
        f"/api/v1/action-steps/{record['id']}/executions",
        json={
            "expected_incident_version": record["incident_version"],
            "idempotency_key": "candidate-decision-cannot-execute-119",
        },
        headers=auth_header("executor"),
    )
    assert execute.status_code == 404, execute.text


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_concurrent_exact_replay_and_full_command_subject_conflicts_are_audited(wait_context):
    c = wait_context
    await ready(c)
    approver = next(item for item in c.settings.credentials if item.credential_id == "approver")
    c.app.state.operator_auth_settings = c.settings.model_copy(
        update={
            "credentials": (
                *c.settings.credentials,
                approver.model_copy(
                    update={"credential_id": "approver-two", "subject_id": "second-approver"}
                ),
            )
        }
    )
    results = await asyncio.gather(decide(c), decide(c))
    assert sorted(item.status_code for item in results) == [200, 201]
    assert results[0].json()["decision"] == results[1].json()["decision"]
    for change in (
        {"reason": "同幂等键修改原确认原因"},
        {"request_id": str(uuid4())},
        {"plan_content_sha256": "a" * 64},
        {"expected_incident_version": c.request_record["incident_version"] + 1},
        {"idempotency_key": "another-decision-key-119"},
        {"action": "reject", "acknowledge_unresolved_conditions": False},
    ):
        refused = await decide(c, {**c.decision_body, **change})
        assert refused.status_code == 409, refused.text
    other = {"Authorization": f"Bearer approver-two.{token('approver').split('.')[1]}"}
    assert (await decide(c, header=other)).status_code == 409
    alternate = dict(c.decision_body)
    for name in ("reason", "idempotency_key", "plan_content_sha256"):
        alternate[name] = " \n\t\u3000" + alternate[name] + " \n\t\u3000"
    replay = await c.client.post(
        c.decision_base,
        content=json.dumps(alternate, indent=2),
        headers={**auth_header("approver"), "Content-Type": "application/json"},
    )
    assert replay.status_code == 200, replay.text
    page = await c.client.get(c.decision_base + "/commands?limit=2", headers=auth_header("viewer"))
    assert page.status_code == 200 and page.json()["next_cursor"] is not None
    rest = await c.client.get(
        c.decision_base + f"/commands?after_cursor={page.json()['next_cursor']}",
        headers=auth_header("viewer"),
    )
    assert rest.status_code == 200, rest.text
    items = page.json()["items"] + rest.json()["items"]
    assert [item["outcome"] for item in items].count("applied") == 1
    assert [item["outcome"] for item in items].count("replayed") == 2
    assert [item["outcome"] for item in items].count("error") == 7
    assert "raw_body" not in page.text + rest.text
    assert await fact_counts(c) == (1, 10)
    assert len([item for item in await incident_events(c) if item.to_status == "mitigating"]) == 1


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_concurrent_approve_and_reject_have_one_decision_and_incident_transition(
    wait_context,
):
    c = wait_context
    await ready(c)
    before_parent, before_events = await parent_facts(c), await incident_events(c)
    results = await asyncio.gather(
        decide(c),
        decide(c, {**negative_command(c, "reject"), "idempotency_key": "competing-rejection-119"}),
    )
    assert sorted(item.status_code for item in results) == [201, 409]
    winner = next(item for item in results if item.status_code == 201).json()["decision"]
    assert winner["record"]["outcome"] in ("approved", "rejected")
    assert winner["availability"] == (
        "current" if winner["record"]["outcome"] == "approved" else "recorded"
    )
    assert (await get(c)).json() == winner
    assert await fact_counts(c) == (1, 2)
    assert len(await incident_events(c)) == len(before_events) + 1
    assert await parent_facts(c) == before_parent


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_permissions_strict_body_and_legacy_mode_require_trusted_approver(wait_context):
    c = wait_context
    await ready(c)
    for path in (c.decision_base, c.decision_base + "/commands"):
        assert (await c.client.get(path)).status_code == 401
        assert (await c.client.get(path, headers=auth_header("alert_source"))).status_code == 403
    assert (await c.client.post(c.decision_base, json=c.decision_body)).status_code == 401
    for role in ("viewer", "engineer", "executor", "alert_source"):
        assert (await decide(c, header=auth_header(role))).status_code == 403
    for change in (
        {"actor_id": "trusted-approver"},
        {"authorization_id": str(uuid4())},
        {"steps": []},
        {"preconditions": []},
        {"evidence_ids": []},
        {"execution_eligible": True},
        {"expires_at": c.clock().isoformat()},
        {"expected_wait_version": c.request_record["wait_version"]},
        {"expected_incident_version": True},
        {"expected_incident_version": "2"},
        {"expected_incident_version": 0},
        {"acknowledge_unresolved_conditions": False},
        {"acknowledge_unresolved_conditions": 1},
        {"acknowledge_unresolved_conditions": "true"},
        {"action": "reject"},
        {"action": "expire"},
        {"action": "execute"},
        {"reason": "\n \t\u3000"},
        {"reason": "长" * 501},
        {"plan_content_sha256": "not-a-hash"},
        {"request_id": "not-a-uuid"},
    ):
        response = await decide(c, {**c.decision_body, **change})
        assert response.status_code == 422, response.text
    for field in ("action", "request_id", "expected_incident_version", "reason"):
        body = dict(c.decision_body)
        body.pop(field)
        assert (await decide(c, body)).status_code == 422
    body = dict(c.decision_body)
    body.pop("acknowledge_unresolved_conditions")
    assert (await decide(c, body)).status_code == 422
    large = await c.client.post(
        c.decision_base,
        content=json.dumps(c.decision_body) + " " * 65536,
        headers={**auth_header("approver"), "Content-Type": "application/json"},
    )
    assert large.status_code == 413, large.text
    c.app.state.operator_auth_settings = c.settings.model_copy(update={"mode": "legacy_local"})
    assert (await c.client.post(c.decision_base, json=c.decision_body)).status_code == 401
    assert (await decide(c, header=auth_header("engineer"))).status_code == 401
    c.app.state.operator_auth_settings = c.settings
    assert await fact_counts(c) == (0, 0)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_request_hash_version_and_missing_resource_conflicts_are_audited(wait_context):
    c = wait_context
    await ready(c)
    before_events = await incident_events(c)
    for index, change in enumerate(
        (
            {"request_id": str(uuid4())},
            {"plan_content_sha256": "f" * 64},
            {"expected_incident_version": c.request_record["previous_incident_version"]},
        )
    ):
        response = await decide(
            c, {**c.decision_body, "idempotency_key": f"invalid-decision-119-{index}", **change}
        )
        assert response.status_code == 409, response.text
    missing = await c.client.post(
        c.decision_base.replace(str(c.job_id), str(uuid4())),
        json=c.decision_body,
        headers=auth_header("approver"),
    )
    assert missing.status_code == 404, missing.text
    assert await fact_counts(c) == (0, 4)
    assert len(await incident_events(c)) == len(before_events)
    page = await c.client.get(c.decision_base + "/commands", headers=auth_header("viewer"))
    assert page.status_code == 200, page.text
    assert len(page.json()["items"]) == 3
    assert all(item["outcome"] == "error" for item in page.json()["items"])
    assert (await decide(c)).status_code == 201


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("action", ["reject", "expire"])
async def test_negative_decisions_converge_without_reopening_parent_or_extending_deadline(
    wait_context, action
):
    c = wait_context
    await ready(c)
    before_parent, before_events = await parent_facts(c), await incident_events(c)
    body = negative_command(c, action)
    # 拒绝和过期默认不确认未决条件，不能继承批准语义。
    body.pop("acknowledge_unresolved_conditions")
    if action == "expire":
        early = await decide(c, body)
        assert early.status_code == 409, early.text
        c.clock.current = datetime.fromisoformat(c.request_record["expires_at"])
        counts = await fact_counts(c)
        assert (await get(c)).json()["record"] is None
        assert (await get_request(c)).json()["availability"] == "expired"
        assert await fact_counts(c) == counts
        for late in (c.decision_body, negative_command(c, "reject")):
            refused = await decide(c, late)
            assert refused.status_code == 409, refused.text
        assert await fact_counts(c) == (0, 3)
    response = await decide(c, body)
    assert response.status_code == 201, response.text
    view = response.json()["decision"]
    record = view["record"]
    assert view["availability"] == "recorded"
    assert record["outcome"] == ("rejected" if action == "reject" else "expired")
    assert record["content"]["execution_eligible"] is False
    assert record["previous_incident_version"] == c.request_record["incident_version"]
    assert record["incident_version"] == c.request_record["incident_version"] + 1
    assert (await get(c)).json() == view
    c.clock.current = datetime.fromisoformat(c.request_record["expires_at"]) + timedelta(seconds=1)
    replay = await decide(c, body)
    assert replay.status_code == 200 and replay.json() == {"applied": False, "decision": view}
    assert await parent_facts(c) == before_parent
    after_events = await incident_events(c)
    assert len(after_events) == len(before_events) + 1
    assert after_events[-1].id == record["event_cursor"]
    assert (after_events[-1].from_status, after_events[-1].to_status) == (
        "awaiting_approval",
        "investigating",
    )
    assert after_events[-1].payload == {
        "action": "reject_plan" if action == "reject" else "expire_approval"
    }
    async with c.db() as session:
        incident = await session.get(IncidentRow, c.started.run.incident_id)
        assert incident.status == "investigating" and incident.version == record["incident_version"]
    assert (await get_request(c)).json()["record"] == c.request_record


@pytest.mark.parametrize("wait_context", ["human_clean_with_change"], indirect=True)
@pytest.mark.parametrize("input_action", ["supplement", "cancel"])
@pytest.mark.parametrize("decision_action", ["reject", "expire"])
async def test_changed_or_cancelled_source_refuses_approval_but_allows_negative_convergence(
    wait_context, input_action, decision_action
):
    c = wait_context
    await ready(c, kind="human_route")
    changed = await post(
        c, command(c, action=input_action, version=2, key="decision-source-change-119")
    )
    assert changed.status_code == 201, changed.text
    assert (await get_request(c)).json()["availability"] == "stale"
    before_parent, before_events = await parent_facts(c), await incident_events(c)
    refused = await decide(c)
    assert refused.status_code == 409, refused.text
    assert await fact_counts(c) == (0, 1)
    if decision_action == "expire":
        c.clock.current = datetime.fromisoformat(c.request_record["expires_at"])
    body = negative_command(c, decision_action)
    response = await decide(c, body)
    assert response.status_code == 201, response.text
    view = response.json()["decision"]
    assert view["availability"] == "recorded"
    assert view["record"]["outcome"] == ("rejected" if decision_action == "reject" else "expired")
    replay = await decide(c, body)
    assert replay.status_code == 200 and replay.json() == {"applied": False, "decision": view}
    assert await parent_facts(c) == before_parent
    assert len(await incident_events(c)) == len(before_events) + 1
    async with c.db() as session:
        incident = await session.get(IncidentRow, c.started.run.incident_id)
        assert incident.status == "investigating"
        assert incident.version == c.request_record["incident_version"] + 1
    assert (await get_request(c)).json()["record"] == c.request_record


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_approved_history_becomes_stale_and_expired_without_read_side_writes(wait_context):
    c = wait_context
    await ready(c)
    response = await decide(c)
    assert response.status_code == 201, response.text
    record = response.json()["decision"]["record"]
    assert (await get(c)).json()["availability"] == "current"
    async with c.db() as session, session.begin():
        await session.execute(
            text("UPDATE incidents SET version=version+1 WHERE id=:id"),
            {"id": c.started.run.incident_id},
        )
    counts, before_parent = await fact_counts(c), await parent_facts(c)
    for expected in ("stale", "expired"):
        if expected == "expired":
            c.clock.current = datetime.fromisoformat(c.request_record["expires_at"])
        view = {"record": record, "availability": expected}
        assert (await get(c)).json() == view
        assert await fact_counts(c) == counts
        replay = await decide(c)
        assert replay.status_code == 200 and replay.json() == {"applied": False, "decision": view}
        counts = await fact_counts(c)
    assert await parent_facts(c) == before_parent
    changed = await decide(c, {**c.decision_body, "idempotency_key": "new-after-history-119"})
    assert changed.status_code == 409, changed.text


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("phase", ["approve_deadline", "reject_deadline", "expire_auth"])
async def test_write_side_deadline_and_auth_rechecks_roll_back_all_business_facts(
    wait_context, phase
):
    c = wait_context
    await ready(c)
    before_parent, before_events = await parent_facts(c), await incident_events(c)
    action = phase.split("_")[0]
    if action == "expire":
        c.clock.current = datetime.fromisoformat(c.request_record["expires_at"])
    body = c.decision_body if action == "approve" else negative_command(c, action)
    advanced = False

    def advance_after_flush(session, flush_context):
        nonlocal advanced
        if not advanced and any(
            row.__tablename__ == "candidate_decision_commands" and row.outcome == "applied"
            for row in session.new
        ):
            advanced = True
            c.clock.current = (
                c.clock() + timedelta(days=2)
                if phase == "expire_auth"
                else datetime.fromisoformat(c.request_record["expires_at"])
            )

    event.listen(Session, "after_flush", advance_after_flush)
    try:
        response = await decide(c, body)
    finally:
        event.remove(Session, "after_flush", advance_after_flush)
    assert advanced
    assert response.status_code == (401 if phase == "expire_auth" else 409), response.text
    assert await fact_counts(c) == (0, 0)
    assert len(await incident_events(c)) == len(before_events)
    assert await parent_facts(c) == before_parent
    async with c.db() as session:
        incident = await session.get(IncidentRow, c.started.run.incident_id)
        assert incident.status == "awaiting_approval"
        assert incident.version == c.request_record["incident_version"]


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_auth_expired_after_authorization_lock_refuses_before_business_writes(
    wait_context, monkeypatch
):
    c = wait_context
    await ready(c)
    before_events = await incident_events(c)
    original, advanced = AsyncSession.get, False

    async def acquire(self, entity, *args, **kwargs):
        nonlocal advanced
        row = await original(self, entity, *args, **kwargs)
        if entity is OperatorAuthorizationRow and kwargs.get("with_for_update") and not advanced:
            advanced = True
            c.clock.current += timedelta(days=2)
        return row

    monkeypatch.setattr(AsyncSession, "get", acquire)
    response = await decide(c)
    assert advanced and response.status_code == 401, response.text
    assert response.json()["error_code"] == "operator_unauthenticated"
    assert await fact_counts(c) == (0, 0)
    assert len(await incident_events(c)) == len(before_events)
