"""精确核验版本的新审批闭环、可信边界与原事实隔离。"""

import asyncio
import copy
import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from tests.api.test_candidate_approval_api import get as get_old_request
from tests.api.test_candidate_approval_api import incident_events as read_incident_events
from tests.api.test_candidate_conditions_api import canonical_hash, following, replace
from tests.api.test_candidate_conditions_api import create as verify_conditions
from tests.api.test_candidate_conditions_api import get as get_conditions
from tests.api.test_candidate_conditions_api import ready as ready_conditions
from tests.api.test_candidate_decision_api import get as get_old_decision
from tests.api.test_candidate_decision_api import parent_facts
from tests.api.test_candidate_plan_api import get as get_plan
from tests.api.test_candidate_preflight_api import create as create_preflight
from tests.api.test_candidate_preflight_api import get as get_preflights
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.identity_helpers import auth_header, token

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def incident_events(c):
    # 不同会话返回不同ORM实例；比较持久列，才能验证没有追加或改写事故事件。
    return tuple(
        tuple(getattr(row, column.key) for column in row.__table__.columns)
        for row in await read_incident_events(c)
    )


def bind_verification(c, record):
    c.verification_record = record
    c.condition_approval_base = f"{c.conditions_base}/versions/{record['version']}/approval-request"
    c.condition_request_body = dict(
        verification_id=record["id"],
        verification_content_sha256=record["content_sha256"],
        expected_incident_version=record["incident_version"],
        idempotency_key=f"condition-request-{record['version']}-122",
        reason="请求独立审查当前完整核验，不能沿用旧批准",
    )


async def ready(c, *, kind="terminal_handoff"):
    from opsswarm.approval.conditions.store import ConditionApprovalStore

    await ready_conditions(c, kind=kind)
    c.app.state.condition_approval_store = ConditionApprovalStore(c.db, clock=c.clock)
    response = await verify_conditions(c)
    assert response.status_code == 201, response.text
    bind_verification(c, response.json()["verification"]["record"])


async def create_request(c, body=None, *, header=None, base=None):
    return await c.client.post(
        base or c.condition_approval_base,
        json=c.condition_request_body if body is None else body,
        headers=header or auth_header("engineer"),
    )


def decision_body(c, record, *, action="approve", key="condition-decision-122"):
    return dict(
        action=action,
        request_id=record["id"],
        request_content_sha256=record["content_sha256"],
        expected_incident_version=record["incident_version"],
        acknowledge_verification=action == "approve",
        idempotency_key=key,
        reason="审批者明确审查精确核验版本并保留原来源限制",
    )


async def decide(c, body, *, header=None, base=None):
    return await c.client.post(
        (base or c.condition_approval_base) + "/decision",
        json=body,
        headers=header or auth_header("approver"),
    )


async def get(c, suffix="", *, base=None):
    return await c.client.get(
        (base or c.condition_approval_base) + suffix, headers=auth_header("viewer")
    )


async def fact_counts(c):
    async with c.db() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in (
                    "condition_approval_requests",
                    "condition_approval_decisions",
                    "condition_approval_commands",
                )
            ]
        )


async def append_verification(c, *, status="verified"):
    response = await verify_conditions(c, following(c, c.verification_record, status=status))
    assert response.status_code == 201, response.text
    bind_verification(c, response.json()["verification"]["record"])


async def occupy_budget(c, *, status="unknown"):
    """追加无业务结果的调用审计，不篡改原成功调用用量或冻结来源。"""
    async with c.db() as session, session.begin():
        await session.execute(text("SET LOCAL session_replication_role=replica"))
        await session.execute(
            text("""
                INSERT INTO investigation_call_reservations
                SELECT extra.* FROM investigation_call_reservations source
                CROSS JOIN generate_series(40,:last_slot) slot
                CROSS JOIN LATERAL jsonb_populate_record(
                  NULL::investigation_call_reservations,
                  to_jsonb(source)||jsonb_build_object(
                    'id',gen_random_uuid(),'candidate_attempt_id',NULL,
                    'human_review_attempt_id',NULL,'task_id',NULL,'task_attempt_id',NULL,
                    'operation','synthesize','invocation_index',slot,
                    'sequence',(SELECT max(sequence) FROM investigation_call_reservations
                                WHERE investigation_run_id=:run)+slot-39,
                    'idempotency_key',source.investigation_attempt_id::text||
                                      ':root:synthesize:'||slot,
                    'status',CAST(:status AS text),'error_code',CAST(:error AS text),
                    'model_usage',NULL,'call_result',NULL,'context_manifest',NULL,
                    'reserved_at',:now,'finished_at',CAST(:finished AS timestamptz))) extra
                WHERE source.investigation_run_id=:run AND source.operation='evaluate_candidate'
            """),
            {
                "run": c.started.run.id,
                "now": c.clock(),
                "last_slot": 40 if status == "reserved" else 59,
                "status": status,
                "error": None if status == "reserved" else "operation_failed",
                "finished": None if status == "reserved" else c.clock(),
            },
        )
        await session.execute(text("SET LOCAL session_replication_role=origin"))


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_create_approve_replay_without_mutating_original_facts(
    wait_context, kind
):
    c = wait_context
    await ready(c, kind=kind)
    preflight = await create_preflight(c)
    assert preflight.status_code == 201, preflight.text
    before_parent, before_events = await parent_facts(c), await incident_events(c)
    assert (await get(c, "/decision")).status_code == 404
    first = await create_request(c)
    assert first.status_code == 201, first.text
    request = first.json()["request"]["record"]
    assert first.json()["request"]["availability"] == "current"
    assert request["id"] == str(
        uuid5(UUID(c.verification_record["id"]), "condition-approval-request:v1")
    )
    assert request["verification_id"] == c.verification_record["id"]
    assert request["verification_version"] == 1
    assert request["verification_content_sha256"] == c.verification_record["content_sha256"]
    assert request["incident_version"] == c.decision_record["incident_version"]
    assert request["status"] == "pending" and request["requested_by_id"] == "trusted-engineer"
    assert request["expires_at"] == c.verification_record["expires_at"]
    assert request["content_sha256"] == canonical_hash(request["content"])
    for field, value in (
        ("base_decision_id", c.decision_record["id"]),
        ("base_decision_content_sha256", c.decision_record["content_sha256"]),
        ("plan_id", c.plan_record["id"]),
        ("plan_content_sha256", c.plan_record["content_sha256"]),
    ):
        assert request["content"][field] == value
    assert request["content"]["execution_eligible"] is False
    assert (await get(c, "/decision")).json() == {"record": None, "availability": "pending"}
    command = decision_body(c, request)
    approved = await decide(c, command)
    assert approved.status_code == 201, approved.text
    view = approved.json()["decision"]
    decision = view["record"]
    assert view["availability"] == "current" and decision["outcome"] == "approved"
    assert decision["id"] == str(uuid5(UUID(request["id"]), "condition-approval-decision:v1"))
    assert decision["request_content_sha256"] == request["content_sha256"]
    assert decision["incident_version"] == request["incident_version"]
    assert decision["content_sha256"] == canonical_hash(decision["content"])
    assert decision["content"]["execution_eligible"] is False
    assert (await get(c)).json() == {"record": request, "availability": "recorded"}
    assert (await get(c, "/decision")).json() == view
    assert (await create_request(c)).json() == {
        "applied": False,
        "request": {"record": request, "availability": "recorded"},
    }
    assert (await decide(c, command)).json() == {"applied": False, "decision": view}
    assert (await get_conditions(c)).json()["latest"] == c.verification_record
    assert (await get_plan(c)).json()["record"] == c.plan_record
    assert (await get_old_request(c)).json()["record"] == c.request_record
    assert (await get_old_decision(c)).json()["record"] == c.decision_record
    assert (await get_preflights(c)).json()["items"] == [preflight.json()["record"]]
    assert await parent_facts(c) == before_parent
    assert await incident_events(c) == before_events
    async with c.db() as session:
        incident = await session.get(IncidentRow, c.started.run.incident_id)
        assert incident.status == "mitigating" and incident.version == request["incident_version"]
        for record, role, operation in (
            (request, "engineer", "create_condition_approval_request"),
            (decision, "approver", "decide_condition_approval"),
        ):
            admission = await session.get(
                OperatorAuthorizationRow, UUID(record["authorization_id"])
            )
            assert admission.role == role and admission.operation == operation
            assert admission.resources == {
                "run_id": str(c.started.run.id),
                "job_id": str(c.job_id),
                "version": "1",
            }
        for table in ("action_plans", "action_steps", "approval_requests", "operation_executions"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    page = (await get(c, "/commands?limit=2")).json()
    assert page["next_cursor"] is not None
    rest = (await get(c, f"/commands?after_cursor={page['next_cursor']}")).json()
    assert [(item["stage"], item["outcome"]) for item in page["items"] + rest["items"]] == [
        ("request", "applied"),
        ("decision", "applied"),
        ("request", "replayed"),
        ("decision", "replayed"),
    ]


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_revisions_supersede_approval_and_block_non_ready_requests(wait_context):
    c = wait_context
    await ready(c)
    first = await create_request(c)
    request = first.json()["request"]["record"]
    command = decision_body(c, request)
    assert (await decide(c, command)).status_code == 201
    original_base = c.condition_approval_base
    original_body = copy.deepcopy(c.condition_request_body)
    for status in ("unresolved", "not_applicable"):
        await append_verification(c, status=status)
        assert (await create_request(c)).status_code == 409
        assert (await get(c, "/decision", base=original_base)).json()[
            "availability"
        ] == "superseded"
        assert (await decide(c, command, base=original_base)).json()["decision"][
            "availability"
        ] == "superseded"
    await append_verification(c)
    replacement = await create_request(c)
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["request"]["record"]["id"] != request["id"]
    assert (await create_request(c, original_body, base=original_base)).json()["applied"] is False
    assert (await get_conditions(c)).json()["condition_version"] == 4
    latest_base = c.condition_approval_base
    latest_request = replacement.json()["request"]["record"]
    # 两入口共享父锁：追加可先阻止批准，或在批准后令其即时失效。
    competing = await asyncio.gather(
        decide(c, decision_body(c, latest_request)),
        verify_conditions(c, following(c, c.verification_record)),
    )
    assert competing[1].status_code == 201, competing[1].text
    assert competing[0].status_code in (201, 409), competing[0].text
    outcome = await get(c, "/decision", base=latest_base)
    assert outcome.status_code == 200, outcome.text
    assert outcome.json()["availability"] == (
        "superseded" if competing[0].status_code == 201 else "pending"
    )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_request_and_decision_concurrency_full_command_and_subject_conflicts(wait_context):
    c = wait_context
    await ready(c)
    responses = await asyncio.gather(create_request(c), create_request(c))
    assert sorted(item.status_code for item in responses) == [200, 201]
    request = responses[0].json()["request"]["record"]
    assert (
        await create_request(c, {**c.condition_request_body, "reason": "同键不同正文"})
    ).status_code == 409
    other_engineer = {"Authorization": f"Bearer engineer-two.{token('engineer').split('.')[1]}"}
    assert (await create_request(c, header=other_engineer)).status_code == 409
    command = decision_body(c, request)
    competitors = await asyncio.gather(
        decide(c, command),
        decide(c, decision_body(c, request, action="reject", key="competing-reject-122")),
    )
    assert sorted(item.status_code for item in competitors) == [201, 409]
    winner = next(item for item in competitors if item.status_code == 201).json()["decision"]
    winner_command = (
        command
        if winner["record"]["outcome"] == "approved"
        else decision_body(c, request, action="reject", key="competing-reject-122")
    )
    exact = await asyncio.gather(decide(c, winner_command), decide(c, winner_command))
    assert all(item.status_code == 200 for item in exact)
    assert all(item.json()["decision"] == winner for item in exact)
    assert (await decide(c, {**winner_command, "reason": "相同幂等键改变正文"})).status_code == 409
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
    other = {"Authorization": f"Bearer approver-two.{token('approver').split('.')[1]}"}
    assert (await decide(c, winner_command, header=other)).status_code == 409
    assert (await fact_counts(c))[:2] == (1, 1)
    page = await get(c, "/commands")
    assert page.status_code == 200 and "raw_body" not in page.text
    assert len([item for item in page.json()["items"] if item["outcome"] == "applied"]) == 2


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_trusted_roles_strict_commands_path_and_three_resource_binding(wait_context):
    c = wait_context
    await ready(c)
    for suffix in ("", "/decision", "/commands"):
        assert (await c.client.get(c.condition_approval_base + suffix)).status_code == 401
        assert (
            await c.client.get(
                c.condition_approval_base + suffix, headers=auth_header("alert_source")
            )
        ).status_code == 403
    assert (
        await c.client.post(c.condition_approval_base, json=c.condition_request_body)
    ).status_code == 401
    for role in ("viewer", "approver", "executor", "alert_source"):
        assert (await create_request(c, header=auth_header(role))).status_code == 403
    for change in (
        {"actor_id": "forged"},
        {"authorization_id": str(uuid4())},
        {"expected_incident_version": True},
        {"expected_incident_version": "3"},
        {"verification_content_sha256": "invalid"},
        {"execution_eligible": True},
        {"reason": " \n\t"},
        {"expires_at": c.clock().isoformat()},
    ):
        assert (await create_request(c, {**c.condition_request_body, **change})).status_code == 422
    for version in ("0", "9", "01", "+1", "1.0"):
        invalid = c.condition_approval_base.replace("/versions/1/", f"/versions/{version}/")
        assert (await get(c, base=invalid)).status_code == 422
    wrong = c.condition_approval_base.replace("/versions/1/", "/versions/2/")
    assert (await create_request(c, base=wrong)).status_code == 404
    for index, change in enumerate(
        (
            {"verification_id": str(uuid4())},
            {"verification_content_sha256": "f" * 64},
            {"expected_incident_version": c.verification_record["incident_version"] + 1},
        )
    ):
        result = await create_request(
            c, {**c.condition_request_body, **change, "idempotency_key": f"bad-request-{index}-122"}
        )
        assert result.status_code == 409, result.text
    request = (await create_request(c)).json()["request"]["record"]
    body = decision_body(c, request)
    for role in ("viewer", "engineer", "executor", "alert_source"):
        assert (await decide(c, body, header=auth_header(role))).status_code == 403
    for change in (
        {"acknowledge_verification": False},
        {"acknowledge_verification": 1},
        {"acknowledge_verification": "true"},
        {"action": "reject"},
        {"expected_incident_version": True},
        {"actor_id": "forged"},
        {"request_content_sha256": "invalid"},
        {"execution_eligible": True},
    ):
        assert (await decide(c, {**body, **change})).status_code == 422
    for index, change in enumerate(
        (
            {"request_id": str(uuid4())},
            {"request_content_sha256": "f" * 64},
            {"expected_incident_version": request["incident_version"] + 1},
        )
    ):
        assert (
            await decide(c, {**body, **change, "idempotency_key": f"bad-decision-{index}-122"})
        ).status_code == 409
    for base, payload, role in (
        (c.condition_approval_base, c.condition_request_body, "engineer"),
        (c.condition_approval_base + "/decision", body, "approver"),
    ):
        large = await c.client.post(
            base,
            content=json.dumps(payload) + " " * 65536,
            headers={**auth_header(role), "Content-Type": "application/json"},
        )
        assert large.status_code == 413
    c.app.state.operator_auth_settings = c.settings.model_copy(update={"mode": "legacy_local"})
    assert (
        await c.client.post(c.condition_approval_base, json=c.condition_request_body)
    ).status_code == 401
    assert (
        await c.client.post(c.condition_approval_base + "/decision", json=body)
    ).status_code == 401
    c.app.state.operator_auth_settings = c.settings
    assert (await decide(c, body)).status_code == 201


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize(
    ("action", "budget_status"), [("reject", "unknown"), ("expire", "unknown")]
)
async def test_negative_decisions_accept_stale_superseded_budget_without_incident_mutation(
    wait_context, action, budget_status
):
    c = wait_context
    await ready(c)
    request = (await create_request(c)).json()["request"]["record"]
    old_base = c.condition_approval_base
    command = decision_body(c, request, action=action)
    if action == "expire":
        assert (await decide(c, command)).status_code == 409
    await append_verification(c)
    await occupy_budget(c, status=budget_status)
    async with c.db() as session, session.begin():
        await session.execute(
            text("UPDATE incidents SET version=version+1 WHERE id=:id"),
            {"id": c.started.run.incident_id},
        )
    if action == "expire":
        c.clock.current = datetime.fromisoformat(request["expires_at"])
        assert (await decide(c, decision_body(c, request), base=old_base)).status_code == 409
        assert (
            await decide(
                c, decision_body(c, request, action="reject", key="late-reject-122"), base=old_base
            )
        ).status_code == 409
    else:
        assert (await decide(c, decision_body(c, request), base=old_base)).status_code == 409
    before_parent, before_events = await parent_facts(c), await incident_events(c)
    command.pop("acknowledge_verification")
    result = await decide(c, command, base=old_base)
    assert result.status_code == 201, result.text
    view = result.json()["decision"]
    assert view["availability"] == "recorded"
    assert view["record"]["outcome"] == ("rejected" if action == "reject" else "expired")
    assert view["record"]["incident_version"] == request["incident_version"]
    assert (await get(c, base=old_base)).json() == {"record": request, "availability": "recorded"}
    assert (await decide(c, command, base=old_base)).json() == {"applied": False, "decision": view}
    assert await parent_facts(c) == before_parent and await incident_events(c) == before_events


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_current_budget_gate_and_expiry_do_not_renew_request_or_approved_decision(
    wait_context,
    monkeypatch,
):
    c = wait_context
    await ready(c)
    request = (await create_request(c)).json()["request"]["record"]
    command = decision_body(c, request)
    from opsswarm.approval.conditions import sources

    read_snapshot = sources.read_budget_snapshot

    async def in_flight(session, run):
        snapshot = await read_snapshot(session, run)
        # 只注入容量投影，不能把不再持租约的旧父调查伪造成新在途调用来源。
        reserved = snapshot.calls[-1].model_copy(
            update={"status": "reserved", "finished_at": None, "model_usage": None}
        )
        return snapshot.model_copy(update={"calls": (*snapshot.calls, reserved)})

    with monkeypatch.context() as patch:
        patch.setattr(sources, "read_budget_snapshot", in_flight)
        assert (await get(c)).json()["availability"] == "stale"
        assert (await decide(c, command)).status_code == 409
    await occupy_budget(c)
    assert (await get(c)).json()["availability"] == "stale"
    assert (await decide(c, command)).status_code == 409
    async with c.db() as session, session.begin():
        await session.execute(text("SET LOCAL session_replication_role=replica"))
        await session.execute(
            text(
                "DELETE FROM investigation_call_reservations "
                "WHERE investigation_run_id=:run AND status='unknown' "
                "AND operation='synthesize' AND invocation_index BETWEEN 40 AND 59"
            ),
            {"run": c.started.run.id},
        )
        await session.execute(text("SET LOCAL session_replication_role=origin"))
    command["idempotency_key"] = "approve-after-settled-budget-122"
    approved = await decide(c, command)
    assert approved.status_code == 201, approved.text
    record = approved.json()["decision"]["record"]
    c.clock.current = datetime.fromisoformat(request["expires_at"])
    assert (await get(c, "/decision")).json() == {"record": record, "availability": "expired"}
    assert (await decide(c, command)).json()["decision"] == {
        "record": record,
        "availability": "expired",
    }
    assert (
        await decide(
            c, decision_body(c, request, action="expire", key="cannot-replace-approve-122")
        )
    ).status_code == 409
    assert (await create_request(c)).json()["request"]["record"]["expires_at"] == request[
        "expires_at"
    ]


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize(
    ("stage", "phase"),
    [("request", "deadline"), ("request", "auth"), ("decision", "deadline"), ("decision", "auth")],
)
async def test_write_after_flush_rechecks_rollback_facts_and_applied_audit(
    wait_context, stage, phase
):
    c = wait_context
    await ready(c)
    if stage == "decision":
        request = (await create_request(c)).json()["request"]["record"]
        command = decision_body(c, request)
    before_counts = await fact_counts(c)
    before_parent, before_events = await parent_facts(c), await incident_events(c)
    advanced = False

    def advance(session, flush_context):
        nonlocal advanced
        if not advanced and any(
            row.__tablename__ == "condition_approval_commands"
            and row.outcome == "applied"
            and row.stage == stage
            for row in session.new
        ):
            advanced = True
            c.clock.current = (
                datetime.fromisoformat(c.verification_record["expires_at"])
                if phase == "deadline"
                else c.clock() + timedelta(days=2)
            )

    event.listen(Session, "after_flush", advance)
    try:
        result = await create_request(c) if stage == "request" else await decide(c, command)
    finally:
        event.remove(Session, "after_flush", advance)
    assert advanced and result.status_code == (409 if phase == "deadline" else 401), result.text
    assert await fact_counts(c) == before_counts
    assert await parent_facts(c) == before_parent and await incident_events(c) == before_events


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_corrupt_highest_verification_or_source_refuses_all_history_and_replay(wait_context):
    c = wait_context
    await ready(c)
    request = (await create_request(c)).json()["request"]["record"]
    command = decision_body(c, request)
    approved = await decide(c, command)
    assert approved.status_code == 201
    decision = approved.json()["decision"]["record"]
    old_base, old_body = c.condition_approval_base, copy.deepcopy(c.condition_request_body)
    await append_verification(c)
    highest = c.verification_record
    before = await fact_counts(c)
    async with c.db() as session:
        source_hash = await session.scalar(
            text(
                "SELECT source_content_sha256 FROM investigation_candidate_evaluation_jobs "
                "WHERE id=:id"
            ),
            {"id": c.job_id},
        )
        request_raw = await session.scalar(
            text("SELECT raw_body FROM condition_approval_commands WHERE authorization_id=:id"),
            {"id": UUID(request["authorization_id"])},
        )
    original_resources = {
        "run_id": str(c.started.run.id),
        "job_id": str(c.job_id),
        "version": "1",
    }
    attacks = (
        (
            "UPDATE candidate_condition_verifications SET content_sha256=:value WHERE id=:id",
            UUID(highest["id"]),
            "f" * 64,
            highest["content_sha256"],
        ),
        (
            "UPDATE candidate_condition_verifications SET content=CAST(:value AS jsonb) "
            "WHERE id=:id",
            UUID(highest["id"]),
            json.dumps({**highest["content"], "execution_eligible": 0}),
            json.dumps(highest["content"]),
        ),
        (
            "UPDATE investigation_candidate_evaluation_jobs SET source_content_sha256=:value "
            "WHERE id=:id",
            c.job_id,
            "f" * 64,
            source_hash,
        ),
        (
            "UPDATE operator_authorizations SET resources=CAST(:value AS jsonb) WHERE id=:id",
            UUID(request["authorization_id"]),
            json.dumps(
                {key: value for key, value in original_resources.items() if key != "version"}
            ),
            json.dumps(original_resources),
        ),
        (
            "UPDATE operator_authorizations SET resources=CAST(:value AS jsonb) WHERE id=:id",
            UUID(request["authorization_id"]),
            json.dumps({**original_resources, "version": "2"}),
            json.dumps(original_resources),
        ),
        (
            "UPDATE condition_approval_commands SET raw_body=:value WHERE authorization_id=:id",
            UUID(request["authorization_id"]),
            json.dumps({**old_body, "reason": "伪造的原始正文"}),
            request_raw,
        ),
        (
            "UPDATE condition_approval_requests SET content=CAST(:value AS jsonb) WHERE id=:id",
            UUID(request["id"]),
            json.dumps({**request["content"], "execution_eligible": 0}),
            json.dumps(request["content"]),
        ),
        (
            "UPDATE condition_approval_decisions SET content_sha256=:value WHERE id=:id",
            UUID(decision["id"]),
            "f" * 64,
            decision["content_sha256"],
        ),
    )
    for index, (sql, identifier, corrupt, original) in enumerate(attacks):
        try:
            await replace(c, sql, {"id": identifier, "value": corrupt})
            for response in (
                await get(c, base=old_base),
                await get(c, "/decision", base=old_base),
                await get(c, "/commands", base=old_base),
                await create_request(c, old_body, base=old_base),
                await decide(c, command, base=old_base),
                await create_request(
                    c, {**old_body, "idempotency_key": "corrupt-request-new-key-122"}, base=old_base
                ),
                await decide(
                    c, {**command, "idempotency_key": "corrupt-decision-new-key-122"}, base=old_base
                ),
            ):
                assert response.status_code == 409, response.text
            # 新版本未引用旧版审批，旧审批损坏不伪装成新核验来源损坏。
            current = await get(c)
            assert current.status_code == (409 if index < 3 else 200), current.text
            assert await fact_counts(c) == before
        finally:
            await replace(c, sql, {"id": identifier, "value": original})
    assert (await get(c, "/decision", base=old_base)).json()["availability"] == "superseded"
    assert (await create_request(c, old_body, base=old_base)).status_code == 200
