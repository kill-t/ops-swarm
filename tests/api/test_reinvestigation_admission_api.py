"""真实收敛来源的可信回流准入、不可变历史及原预算隔离。"""

import asyncio
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from opsswarm.incidents.alerts import AlertSubmission
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.reinvestigation.contracts import ReinvestigationContent
from opsswarm.investigation.reinvestigation.store import CandidateReinvestigationStore
from tests.api import test_candidate_convergence_api as convergence
from tests.api import test_candidate_settlement_api as settlement
from tests.api.test_candidate_conditions_api import replace
from tests.api.test_candidate_convergence_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_candidate_convergence_api import wait_context as wait_context
from tests.identity_helpers import auth_header

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def bind(c):
    c.reinvestigation = CandidateReinvestigationStore(c.db, clock=c.clock)
    c.app.state.candidate_reinvestigation_store = c.reinvestigation
    c.reinvestigation_base = f"/api/v1/investigations/{c.started.run.id}/reinvestigation-admission"


async def prepare(c, kind="terminal_handoff"):
    try:
        await convergence.prepare_settled(c, kind=kind, mode="not_recovered")
    except AssertionError as error:
        if hasattr(c, "recovery_base"):
            recovery = await settlement.recovery_get(c)
            async with c.db() as session:
                facts = {}
                for table in (
                    "investigation_runs",
                    "incidents",
                    "investigation_candidate_evaluation_jobs",
                    "candidate_release_execution_jobs",
                    "candidate_recovery_jobs",
                    "investigation_human_wait_events",
                    "candidate_release_execution_requests",
                    "candidate_condition_verifications",
                ):
                    facts[table] = list(
                        await session.scalars(text(f"SELECT to_jsonb(t) FROM {table} t"))
                    )
                facts["active_calls"] = list(
                    await session.scalars(
                        text(
                            "SELECT to_jsonb(t) FROM investigation_call_reservations t "
                            "WHERE status='reserved'"
                        )
                    )
                )
            raise AssertionError(
                f"回流前置恢复链未完成：clock={c.clock()} recovery={recovery.text} facts={facts}"
            ) from error
        raise
    assert await c.convergence.converge_next()
    await bind(c)


async def command(c, **updates):
    record = (await convergence.get(c)).json()["record"]
    return {
        "action": "request_reinvestigation",
        "convergence_sha256": record["content_sha256"],
        "expected_incident_version": record["after_version"],
        "idempotency_key": "reinvestigation-test-131",
        "reason": "根据隔离演练验证失败申请补充调查",
        **updates,
    }


async def post(c, command=None, headers=None):
    return await c.client.post(
        c.reinvestigation_base,
        json=await globals()["command"](c) if command is None else command,
        headers=auth_header("engineer") if headers is None else headers,
    )


async def get(c, *, headers=None, run_id=None):
    return await c.client.get(
        c.reinvestigation_base
        if run_id is None
        else (f"/api/v1/investigations/{run_id}/reinvestigation-admission"),
        headers=auth_header("viewer") if headers is None else headers,
    )


async def counts(c):
    async with c.db() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in (
                    "candidate_reinvestigation_admissions",
                    "candidate_reinvestigation_commands",
                )
            ]
        )


async def frozen(c):
    async with c.db() as session:
        extra = tuple(
            [
                tuple(
                    await session.scalars(
                        text(f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY to_jsonb(t)::text")
                    )
                )
                for table in (
                    "investigation_runs",
                    "agent_task_events",
                    "candidate_incident_convergences",
                )
            ]
        )
    return await settlement.frozen_facts(c), extra


async def attach_old_alert(c):
    incident = (await c.incidents.get_incident(c.started.run.incident_id)).incident
    record = (await convergence.get(c)).json()["record"]
    received = datetime.fromisoformat(record["created_at"]) - timedelta(microseconds=1)
    return await c.incidents.ingest_alert(
        AlertSubmission(
            source="isolated-reinvestigation-test",
            external_event_id=str(uuid4()),
            fingerprint=incident.fingerprint,
            service_name=incident.service_name,
            severity=incident.severity,
            title="收敛后入库但接收时刻较早的演练告警",
            payload={"simulation": True},
            occurred_at=received,
            ingested_at=received,
        )
    )


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_admit_once_replay_conflicts_and_preserve_full_history(
    wait_context, kind
):
    c = wait_context
    await prepare(c, kind)
    before = await frozen(c)
    assert (await get(c)).json() == {
        "record": None,
        "availability": "pending",
        "blocking_reason_codes": [],
    }
    body = await command(c)
    responses = await asyncio.gather(post(c, body), post(c, body))
    assert sorted(response.status_code for response in responses) == [200, 201], [
        response.text for response in responses
    ]
    record = next(r for r in responses if r.status_code == 201).json()["request"]["record"]
    content = ReinvestigationContent.model_validate(
        {key: value for key, value in record.items() if key != "content_sha256"}
    )
    assert record["content_sha256"] == content_hash(content)
    assert (
        content.root_run_id == content.previous_run_id == content.budget_run_id == c.started.run.id
    )
    assert content.max_reflows == content.reflow_number == 1
    assert content.dispatch_status == "not_dispatched"
    assert content.command_sha256 == content_hash(content.command)
    current = await get(c)
    assert current.headers["cache-control"] == "no-store"
    assert current.json()["availability"] == "current"
    replay = await post(c, body)
    assert replay.status_code == 200 and replay.json()["applied"] is False
    assert replay.json()["request"]["record"] == record
    for changes, headers, code in (
        ({"reason": "同键更换完整正文"}, None, "reinvestigation_idempotency_conflict"),
        ({"idempotency_key": "reflow-different-key"}, None, "reinvestigation_already_exists"),
        (
            {},
            {
                "Authorization": auth_header("engineer")["Authorization"].replace(
                    "Bearer engineer.", "Bearer engineer-two."
                )
            },
            "reinvestigation_already_exists",
        ),
    ):
        response = await post(c, {**body, **changes}, headers=headers)
        assert response.status_code == 409, response.text
        assert response.json()["error_code"] == code
    assert await counts(c) == (1, 6)
    page = await c.client.get(
        c.reinvestigation_base + "/commands?limit=2", headers=auth_header("viewer")
    )
    assert page.status_code == 200, page.text
    assert len(page.json()["items"]) == 2 and page.json()["next_cursor"] is not None
    cursor = page.json()["next_cursor"]
    later = await c.client.get(
        c.reinvestigation_base + f"/commands?after_cursor={cursor}", headers=auth_header("viewer")
    )
    assert later.status_code == 200, later.text
    items = page.json()["items"] + later.json()["items"]
    assert len(items) == 6 and {a["outcome"] for a in items} == {"applied", "replayed", "error"}
    for path in await settlement.history_paths(c, kind):
        response = await c.client.get(path, headers=auth_header("viewer"))
        assert response.status_code == 200, (path, response.text)
    assert await convergence.marker(c) is None and await convergence.active_slot(c) == 1
    assert await frozen(c) == before
    # 告警按持久事件游标判新旧，较早接收时间不能绕过收敛后新事件。
    alert = await attach_old_alert(c)
    assert alert.incident.version == content.incident_version
    current = (await get(c)).json()
    assert current["record"] == record and current["availability"] == "stale"
    assert "new_alert_after_convergence" in current["blocking_reason_codes"]


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_trusted_access_validation_source_damage_and_new_alert_prevent_new_request(
    wait_context,
):
    c = wait_context
    await prepare(c)
    before = await frozen(c)
    body = await command(c)
    for headers, status in (
        ({}, 401),
        (auth_header("viewer"), 403),
        (auth_header("alert_source"), 403),
    ):
        assert (await post(c, body, headers=headers)).status_code == status
    assert (await get(c, headers={})).status_code == 401
    assert (await get(c, headers=auth_header("alert_source"))).status_code == 403
    c.app.state.operator_auth_settings = c.settings.model_copy(update={"mode": "legacy_local"})
    try:
        assert (await post(c, body, headers={})).status_code == 401
        assert (await get(c, headers={})).status_code == 401
    finally:
        c.app.state.operator_auth_settings = c.settings
    assert (await get(c, run_id="not-a-uuid")).status_code == 422
    assert (await get(c, run_id=uuid4())).status_code == 404
    invalid = await post(c, {**body, "expected_incident_version": True})
    assert invalid.status_code == 422
    for changes in (
        {"expected_incident_version": body["expected_incident_version"] + 1},
        {"convergence_sha256": "f" * 64},
    ):
        rejected = await post(c, {**body, **changes})
        assert rejected.status_code == 409, rejected.text
        assert rejected.json()["error_code"] == "reinvestigation_basis_changed"
    sql = (
        "UPDATE candidate_incident_convergences SET content_sha256=:value "
        "WHERE investigation_run_id=:id"
    )
    try:
        await replace(c, sql, {"id": c.started.run.id, "value": "f" * 64})
        damaged = await post(c, body)
        assert damaged.status_code == 409, damaged.text
        assert damaged.json()["error_code"] == "reinvestigation_integrity_violation"
    finally:
        await replace(c, sql, {"id": c.started.run.id, "value": body["convergence_sha256"]})
    assert await counts(c) == (0, 2)
    assert await frozen(c) == before
    alert = await attach_old_alert(c)
    assert alert.incident.version == body["expected_incident_version"]
    response = await post(c, body)
    assert response.status_code == 409, response.text
    assert response.json()["error_code"] == "reinvestigation_basis_changed"
    assert await counts(c) == (0, 3)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_missing_then_recovered_source_cannot_admit(wait_context):
    c = wait_context
    await convergence.prepare_settled(c, mode="healthy")
    await bind(c)
    body = {
        "action": "request_reinvestigation",
        "convergence_sha256": "f" * 64,
        "expected_incident_version": 1,
        "idempotency_key": "missing-source-131",
        "reason": "验证缺失来源应被拒绝",
    }
    missing = await post(c, body)
    assert missing.status_code == 404, missing.text
    assert await c.convergence.converge_next()
    denied = await post(c)
    assert denied.status_code == 409, denied.text
    assert denied.json()["error_code"] == "reinvestigation_source_not_eligible"
    assert await counts(c) == (0, 2)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_unknown_usage_stays_reserved_and_original_deadline_expires_history(wait_context):
    c = wait_context
    await prepare(c)
    # 只模拟供应商未报告用量，保留真实分配、输入和所有来源链。
    async with c.db() as session:
        identifier = await session.scalar(
            text(
                "SELECT id FROM investigation_call_reservations WHERE investigation_run_id=:id "
                "AND kind='model' ORDER BY sequence LIMIT 1"
            ),
            {"id": c.started.run.id},
        )
    await replace(
        c,
        "UPDATE investigation_call_reservations SET model_usage=NULL WHERE id=:id",
        {"id": identifier},
    )
    before = await frozen(c)
    response = await post(c)
    assert response.status_code == 201, response.text
    record = response.json()["request"]["record"]
    budget = record["budget"]
    assert budget["model_usage"]["unknown_model_calls"] >= 1
    assert budget["model_usage"]["reported_cost_usd"] is None
    assert budget["model_usage"]["committed_input_tokens"] > 0
    # 旧任务已经封存，不能把追加调用伪装成尚未定义的回流执行阶段。
    appended_id = uuid4()
    await replace(
        c,
        "INSERT INTO investigation_call_reservations SELECT expanded.* FROM "
        "investigation_call_reservations original CROSS JOIN LATERAL "
        "jsonb_populate_record(NULL::investigation_call_reservations, "
        "to_jsonb(original) || jsonb_build_object('id', CAST(:new_id AS text), "
        "'sequence', (SELECT max(sequence)+1 FROM investigation_call_reservations "
        "WHERE investigation_run_id=:run_id), 'invocation_index', 99, "
        "'idempotency_key', original.investigation_attempt_id::text || ':' || "
        "COALESCE(original.task_attempt_id::text, 'root') || ':' || "
        "original.operation || ':' || '99', "
        "'call_result', NULL, "
        "'status', 'reserved', 'error_code', NULL, 'reserved_at', CAST(:now AS timestamptz), "
        "'finished_at', NULL)) expanded WHERE original.id=(SELECT id FROM "
        "investigation_call_reservations WHERE investigation_run_id=:run_id "
        "AND kind='tool' ORDER BY sequence LIMIT 1)",
        {"new_id": appended_id, "run_id": c.started.run.id, "now": c.clock()},
    )
    try:
        blocked = await get(c)
        assert blocked.status_code == 409, blocked.text
        assert blocked.json()["error_code"] == "reinvestigation_integrity_violation"
    finally:
        await replace(
            c, "DELETE FROM investigation_call_reservations WHERE id=:id", {"id": appended_id}
        )
    restored = await get(c)
    assert restored.status_code == 200, restored.text
    assert restored.json()["record"] == record and restored.json()["availability"] == "current"
    c.clock.live = False
    c.clock.current = datetime.fromisoformat(budget["deadline"])
    view = await get(c)
    assert view.status_code == 200, view.text
    assert view.json()["record"] == record and view.json()["availability"] == "expired"
    assert "run_deadline_exceeded" in view.json()["blocking_reason_codes"]
    # 时钟注入只验证GET当前诊断；命令授权和审计仍使用真实UTC时刻。
    c.clock.live = True
    replay = await post(c)
    assert replay.status_code == 200 and replay.json()["applied"] is False
    assert replay.json()["request"]["record"] == record
    assert await frozen(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_crash_and_expiry_roll_back_request_and_success_audit(
    wait_context,
):
    c = wait_context
    await prepare(c)
    before = await frozen(c)
    body = await command(c)
    touched = False

    def crash(session, context):
        nonlocal touched
        if any(row.__tablename__ == "candidate_reinvestigation_admissions" for row in session.new):
            touched = True
            raise RuntimeError("模拟回流请求flush后提交前崩溃")

    event.listen(Session, "after_flush", crash)
    try:
        with pytest.raises(RuntimeError, match="提交前"):
            await post(c, body)
    finally:
        event.remove(Session, "after_flush", crash)
    assert touched and await counts(c) == (0, 0)
    original = c.clock()
    for target, expected in (("credential", 401), ("deadline", 409)):
        touched = False
        async with c.db() as session:
            started, duration = (
                await session.execute(
                    text(
                        "SELECT started_at, (call_budget->>'max_duration_seconds')::int "
                        "FROM investigation_runs WHERE id=:id"
                    ),
                    {"id": c.started.run.id},
                )
            ).one()
        expiry = next(
            item.expires_at for item in c.settings.credentials if item.credential_id == "engineer"
        )

        advance_to = expiry if target == "credential" else started + timedelta(seconds=duration)

        def expire(session, context, advance_to=advance_to):
            nonlocal touched
            if any(
                row.__tablename__ == "candidate_reinvestigation_admissions" for row in session.new
            ):
                touched = True
                c.clock.live = False
                c.clock.current = advance_to

        event.listen(Session, "after_flush", expire)
        try:
            response = await post(c, body)
        finally:
            event.remove(Session, "after_flush", expire)
            c.clock.current = original
            c.clock.live = True
        assert touched and response.status_code == expected, response.text
        assert await counts(c) == (0, 0)
        assert await frozen(c) == before
    assert (await post(c, body)).status_code == 201
