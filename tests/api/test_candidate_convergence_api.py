"""真实候选来源、事故两跳收敛和活动槽位的原子控制面契约。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from opsswarm.incidents.convergence.store import CandidateIncidentConvergenceStore
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from opsswarm.incidents.alerts import AlertSubmission
from opsswarm.incidents.convergence.contracts import ConvergenceContent
from opsswarm.incidents.persistence.repository import IncidentStore
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.verification.candidate.contracts import ProbeObservation, RecoveryClaim, RecoveryError
from opsswarm.verification.candidate.store import CandidateRecoveryStore
from tests.api import test_candidate_settlement_api as settlement
from tests.api import test_release_execution_jobs_api as execution
from tests.api.test_candidate_conditions_api import replace
from tests.api.test_candidate_settlement_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_candidate_settlement_api import wait_context as wait_context
from tests.identity_helpers import auth_header

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def get(c, *, run_id=None, headers=None):
    return await c.client.get(
        f"/api/v1/investigations/{run_id or c.started.run.id}/incident-convergence",
        headers=auth_header("viewer") if headers is None else headers,
    )


async def bind(c):
    c.convergence = CandidateIncidentConvergenceStore(c.db, clock=c.clock)
    c.app.state.candidate_incident_convergence_store = c.convergence
    c.incidents = IncidentStore(c.db)


async def prepare_settled(c, *, kind="terminal_handoff", mode="healthy"):
    if kind == "terminal_handoff":
        await settlement.prepare(c, execute=mode != "unknown")
    else:
        await execution.ready(c, kind=kind)
        c.clock.live = True
        c.recovery = CandidateRecoveryStore(c.db, clock=c.clock)
        c.app.state.candidate_recovery_store = c.recovery
        c.recovery_base = c.execution_base + "/recovery-verification"
        response = await execution.post(c)
        assert response.status_code == 201, response.text
        assert await execution.worker(c, execution.FakeDispatchClient(c)).run_once()
    if mode == "unknown":
        remote = execution.FakeDispatchClient(c)

        async def revoke():
            c.app.state.operator_auth_settings = c.settings.model_copy(
                update={
                    "credentials": tuple(
                        credential
                        for credential in c.settings.credentials
                        if credential.credential_id != "executor"
                    )
                }
            )

        remote.after_register = revoke
        try:
            assert await execution.worker(c, remote).run_once()
        finally:
            c.app.state.operator_auth_settings = c.settings
    recovery, calls = await settlement.recover(c, mode)
    await settlement.bind(c)
    assert await c.settlement.settle_next()
    await bind(c)
    return recovery, calls


async def snapshots(c):
    """事故与本片marker之外的已验收事实必须逐列保持。"""
    old = (await settlement.frozen_facts(c))[2:]
    async with c.db() as session:
        run = await session.scalar(
            text(
                "SELECT to_jsonb(r)-'candidate_slot_released_at' "
                "FROM investigation_runs r WHERE id=:id"
            ),
            {"id": c.started.run.id},
        )
        extra = tuple(
            [
                tuple(
                    await session.scalars(
                        text(f"SELECT to_jsonb(t)::text FROM {table} t ORDER BY to_jsonb(t)::text")
                    )
                )
                for table in ("investigation_candidate_settlements", "agent_task_events")
            ]
        )
    return old, run, extra


async def marker(c):
    async with c.db() as session:
        return await session.scalar(
            text("SELECT candidate_slot_released_at FROM investigation_runs WHERE id=:id"),
            {"id": c.started.run.id},
        )


async def count(c):
    async with c.db() as session:
        return await session.scalar(text("SELECT count(*) FROM candidate_incident_convergences"))


async def active_slot(c):
    async with c.db() as session:
        return await session.scalar(
            text(
                "SELECT count(*) FROM investigation_runs WHERE incident_id=:id AND "
                "(status IN ('queued','running','await_human','await_candidate') OR "
                "(status='settled' AND candidate_slot_released_at IS NULL))"
            ),
            {"id": c.started.run.incident_id},
        )


async def assert_history(c, kind, *, settlement_body, recovery_body):
    for path in await settlement.history_paths(c, kind):
        historical = await c.client.get(path, headers=auth_header("viewer"))
        assert historical.status_code == 200, (path, historical.text)
    assert (await settlement.get(c)).json() == settlement_body
    assert (await settlement.recovery_get(c)).json() == recovery_body
    for path in (
        c.approval_base + "/decision",
        c.condition_approval_base + "/decision",
        c.release_base,
    ):
        response = await c.client.get(path, headers=auth_header("viewer"))
        assert response.status_code == 200, response.text
        assert response.json()["availability"] != "current"


def checked_record(response):
    assert response.status_code == 200, response.text
    record = response.json()["record"]
    assert record is not None
    content = ConvergenceContent.model_validate(
        {key: value for key, value in record.items() if key != "content_sha256"}
    )
    assert record["content_sha256"] == content_hash(content)
    return record


def expected_fresh_until(recovery):
    chosen = [
        datetime.fromisoformat(sample["observation"]["observed_at"])
        for sample in recovery["samples"]
        if sample["attempt_id"] == recovery["result"]["attempt_id"]
        and sample["observation"] is not None
    ]
    return min(
        datetime.fromisoformat(recovery["record"]["window_end"]),
        min(chosen) + timedelta(seconds=120),
    )


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_converge_once_and_preserve_history_after_later_incident_changes(
    wait_context, kind
):
    c = wait_context
    recovery, calls = await prepare_settled(c, kind=kind)
    assert recovery["result"]["outcome"] == "recovered" and len(calls) == 3
    before = await snapshots(c)
    incident = (await c.incidents.get_incident(c.started.run.incident_id)).incident
    events_before = await c.incidents.list_events(incident.id)
    saved_settlement = (await settlement.get(c)).json()
    assert (await get(c)).json() == {"record": None}
    assert await count(c) == 0 and await active_slot(c) == 1
    assert sorted(
        await asyncio.gather(c.convergence.converge_next(), c.convergence.converge_next())
    ) == [
        False,
        True,
    ]
    response = await get(c)
    record = checked_record(response)
    assert record["outcome"] == "recovered"
    assert record["reason_code"] == "fresh_recovery_verified"
    assert record["investigation_run_id"] == str(c.started.run.id)
    assert record["incident_id"] == str(incident.id)
    assert record["settlement_content_sha256"] == saved_settlement["record"]["content_sha256"]
    assert (record["before_status"], record["before_version"]) == ("mitigating", incident.version)
    assert (record["after_status"], record["after_version"]) == ("resolved", incident.version + 2)
    assert record["slot_released"] is True
    assert datetime.fromisoformat(record["fresh_until"]) == expected_fresh_until(recovery)
    assert record["policy_version"] == "candidate-incident-convergence-v1"
    assert await marker(c) == datetime.fromisoformat(record["created_at"])
    updated = (await c.incidents.get_incident(incident.id)).incident
    assert updated.status == "resolved" and updated.version == incident.version + 2
    assert updated.resolved_at == datetime.fromisoformat(record["created_at"])
    assert updated.closed_at is None
    added = await c.incidents.list_events(incident.id, after_cursor=events_before[-1].cursor)
    assert [(item.event.from_status, item.event.to_status) for item in added] == [
        ("mitigating", "verifying"),
        ("verifying", "resolved"),
    ]
    assert [item.payload for item in added] == [
        {"action": "complete_mitigation"},
        {"action": "confirm_recovery"},
    ]
    assert [item.event.incident_version for item in added] == [
        incident.version + 1,
        incident.version + 2,
    ]
    assert all(
        item.event.actor.actor_type == "system"
        and item.event.actor.actor_id == f"candidate-convergence:{c.started.run.id}"
        for item in added
    )
    assert record["actions"] == ["complete_mitigation", "confirm_recovery"]
    assert await count(c) == 1 and await active_slot(c) == 0
    assert await snapshots(c) == before
    assert not await c.convergence.converge_next()
    assert (await get(c)).json() == response.json()
    await assert_history(c, kind, settlement_body=saved_settlement, recovery_body=recovery)
    last_attempt = recovery["attempts"][-1]
    late_claim = RecoveryClaim(
        job_id=recovery["record"]["id"],
        run_id=c.started.run.id,
        attempt_id=last_attempt["id"],
        number=last_attempt["number"],
        lease_token=last_attempt["lease_token"],
        lease_expires_at=last_attempt["lease_expires_at"],
    )
    with pytest.raises(RecoveryError) as late_sample:
        await c.recovery.save_sample(
            late_claim,
            3,
            observation=ProbeObservation.model_validate(recovery["samples"][-1]["observation"]),
        )
    assert late_sample.value.code == "recovery_lease_lost"
    with pytest.raises(RecoveryError) as late_result:
        await c.recovery.complete(late_claim)
    assert late_result.value.code == "recovery_lease_lost"
    assert await snapshots(c) == before
    rejected = await execution.post(
        c, {**c.execution_body, "idempotency_key": "converged-new-execution-130"}
    )
    assert rejected.status_code == 409, rejected.text
    if kind == "terminal_handoff":
        # 等价于告警请求先接收、等待130持有的事故锁后才入库，不能丢弃该真实告警。
        received_at = datetime.fromisoformat(record["created_at"]) - timedelta(microseconds=1)
        last_observed_at = max(
            datetime.fromisoformat(sample["observation"]["observed_at"])
            for sample in recovery["samples"]
        )
        assert last_observed_at < received_at < datetime.now(UTC)
        alert = await c.incidents.ingest_alert(
            AlertSubmission(
                source="isolated-late-convergence-alert",
                external_event_id=str(uuid4()),
                fingerprint=incident.fingerprint,
                service_name=incident.service_name,
                severity=incident.severity,
                title="等待恢复提交事务后才取得事故锁的演练告警",
                payload={"simulation": True},
                occurred_at=received_at,
                ingested_at=received_at,
            )
        )
        assert alert.incident.id == incident.id and not alert.created_incident
        assert alert.incident.status == "investigating"
        assert alert.incident.version == updated.version + 1
        assert alert.incident.updated_at >= updated.updated_at
        assert alert.incident.resolved_at is None
        assert (await get(c)).json() == response.json()
        await assert_history(c, kind, settlement_body=saved_settlement, recovery_body=recovery)
        assert await snapshots(c) == before
        return
    closed = await c.client.post(
        f"/api/v1/incidents/{incident.id}/actions/close",
        json={"expected_version": updated.version, "reason": "工程师复核事故恢复后关闭"},
        headers=auth_header("engineer"),
    )
    assert closed.status_code == 200, closed.text
    assert closed.json()["status"] == "closed"
    assert (await get(c)).json() == response.json()
    await assert_history(c, kind, settlement_body=saved_settlement, recovery_body=recovery)
    assert await snapshots(c) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_observed_failure_returns_to_investigating_but_retains_original_slot(wait_context):
    c = wait_context
    recovery, calls = await prepare_settled(c, mode="not_recovered")
    assert recovery["result"]["reason_code"] == "business_probe_failed" and len(calls) == 3
    before = await snapshots(c)
    incident = (await c.incidents.get_incident(c.started.run.incident_id)).incident
    events_before = await c.incidents.list_events(incident.id)
    saved_settlement = (await settlement.get(c)).json()
    assert await c.convergence.converge_next()
    response = await get(c)
    record = checked_record(response)
    assert record["outcome"] == "observed_failure" and record["slot_released"] is False
    assert record["reason_code"] == "fresh_verification_failed"
    assert record["actions"] == ["complete_mitigation", "fail_verification"]
    assert (record["after_status"], record["after_version"]) == (
        "investigating",
        incident.version + 2,
    )
    assert datetime.fromisoformat(record["fresh_until"]) == expected_fresh_until(recovery)
    updated = (await c.incidents.get_incident(incident.id)).incident
    assert updated.status == "investigating" and updated.version == incident.version + 2
    assert updated.resolved_at is updated.closed_at is None
    added = await c.incidents.list_events(incident.id, after_cursor=events_before[-1].cursor)
    assert [(item.event.from_status, item.event.to_status) for item in added] == [
        ("mitigating", "verifying"),
        ("verifying", "investigating"),
    ]
    assert [item.payload for item in added] == [
        {"action": "complete_mitigation"},
        {"action": "fail_verification"},
    ]
    assert await marker(c) is None and await active_slot(c) == 1
    assert await snapshots(c) == before
    assert not await c.convergence.converge_next()
    assert (await get(c)).json() == response.json()
    await assert_history(
        c, "terminal_handoff", settlement_body=saved_settlement, recovery_body=recovery
    )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("mode", ["source_failed", "unknown"])
async def test_uncertain_outcomes_never_change_incident_or_release_slot(wait_context, mode):
    c = wait_context
    recovery, calls = await prepare_settled(c, mode=mode)
    assert recovery["result"]["outcome"] == (
        "source_failed" if mode == "source_failed" else "execution_unknown"
    )
    assert len(calls) == (1 if mode == "source_failed" else 0)
    before = await snapshots(c), await c.incidents.get_incident(c.started.run.incident_id)
    events_before = await c.incidents.list_events(c.started.run.incident_id)
    assert await c.convergence.converge_next()
    response = await get(c)
    record = checked_record(response)
    assert record["outcome"] == "held_uncertain"
    assert record["reason_code"] == recovery["result"]["reason_code"]
    assert (record["before_status"], record["before_version"]) == (
        record["after_status"],
        record["after_version"],
    )
    assert record["actions"] == [] and record["slot_released"] is False
    assert record["fresh_until"] is None
    assert await marker(c) is None and await active_slot(c) == 1
    assert (await snapshots(c), await c.incidents.get_incident(c.started.run.incident_id)) == before
    assert await c.incidents.list_events(c.started.run.incident_id) == events_before
    assert not await c.convergence.converge_next()
    assert (await get(c)).json() == response.json()


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_expired_original_evidence_is_held_without_new_samples(wait_context):
    c = wait_context
    recovery, _ = await prepare_settled(c)
    deadline = expected_fresh_until(recovery)
    # 原生约束使用真实数据库时钟，分段等待原证据到期，不改写历史样本。
    while (remaining := (deadline - datetime.now(UTC)).total_seconds()) >= 0:
        await asyncio.sleep(min(30, remaining + 0.05))
    before = await snapshots(c), await c.incidents.get_incident(c.started.run.incident_id)
    assert await c.convergence.converge_next()
    response = await get(c)
    record = checked_record(response)
    assert record["outcome"] == "held_stale"
    assert record["reason_code"] == "recovery_samples_stale"
    assert datetime.fromisoformat(record["fresh_until"]) == deadline
    assert record["actions"] == [] and record["slot_released"] is False
    assert await marker(c) is None and await active_slot(c) == 1
    assert (await snapshots(c), await c.incidents.get_incident(c.started.run.incident_id)) == before


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_new_alert_after_samples_prevents_recovery_without_changing_incident_version(
    wait_context,
):
    c = wait_context
    recovery, _ = await prepare_settled(c)
    incident = (await c.incidents.get_incident(c.started.run.incident_id)).incident
    now = datetime.now(UTC)
    alert = await c.incidents.ingest_alert(
        AlertSubmission(
            source="isolated-convergence-test",
            external_event_id=str(uuid4()),
            fingerprint=incident.fingerprint,
            service_name=incident.service_name,
            severity=incident.severity,
            title="恢复采样后再次出现演练告警",
            payload={"simulation": True},
            occurred_at=now,
            ingested_at=now,
        )
    )
    assert alert.incident.id == incident.id and alert.incident.version == incident.version
    assert not alert.created_incident
    before = await snapshots(c), await c.incidents.get_incident(incident.id)
    events_before = await c.incidents.list_events(incident.id)
    assert await c.convergence.converge_next()
    response = await get(c)
    record = checked_record(response)
    assert record["outcome"] == "held_new_alert"
    assert record["reason_code"] == "new_alert_after_samples"
    assert record["latest_alert_id"] == str(alert.alert_id)
    assert datetime.fromisoformat(record["latest_alert_ingested_at"]) == now
    assert record["actions"] == [] and record["slot_released"] is False
    assert datetime.fromisoformat(record["fresh_until"]) == expected_fresh_until(recovery)
    assert await marker(c) is None and await active_slot(c) == 1
    assert (await snapshots(c), await c.incidents.get_incident(incident.id)) == before
    assert await c.incidents.list_events(incident.id) == events_before
    later = datetime.now(UTC)
    await c.incidents.ingest_alert(
        AlertSubmission(
            source="isolated-convergence-test",
            external_event_id=str(uuid4()),
            fingerprint=incident.fingerprint,
            service_name=incident.service_name,
            severity=incident.severity,
            title="收敛决定后追加的独立演练告警",
            payload={"simulation": True},
            occurred_at=later,
            ingested_at=later,
        )
    )
    assert (await get(c)).json() == response.json()


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_trusted_read_source_corruption_and_crash_roll_back_all_convergence_facts(
    wait_context,
):
    c = wait_context
    recovery, _ = await prepare_settled(c)
    before = await snapshots(c), await c.incidents.get_incident(c.started.run.incident_id)
    events_before = await c.incidents.list_events(c.started.run.incident_id)
    assert (await get(c, headers={})).status_code == 401
    assert (await get(c, headers=auth_header("alert_source"))).status_code == 403
    c.app.state.operator_auth_settings = c.settings.model_copy(update={"mode": "legacy_local"})
    try:
        assert (await get(c, headers={})).status_code == 401
    finally:
        c.app.state.operator_auth_settings = c.settings
    assert (await get(c, run_id="not-a-uuid")).status_code == 422
    assert (await get(c, run_id=uuid4())).status_code == 404
    assert (await get(c)).headers["cache-control"] == "no-store"
    assert (await get(c)).json() == {"record": None}
    identifier = UUID(recovery["record"]["id"])
    for table in ("candidate_recovery_results", "investigation_candidate_settlements"):
        key = "job_id" if table == "candidate_recovery_results" else "recovery_job_id"
        async with c.db() as session:
            digest = await session.scalar(
                text(f"SELECT content_sha256 FROM {table} WHERE {key}=:id"), {"id": identifier}
            )
        sql = f"UPDATE {table} SET content_sha256=:value WHERE {key}=:id"
        try:
            await replace(c, sql, {"id": identifier, "value": "f" * 64})
            with pytest.raises(ValueError):
                await c.convergence.converge_next()
            assert await count(c) == 0 and await marker(c) is None
        finally:
            await replace(c, sql, {"id": identifier, "value": digest})
    flushed = False

    def crash(session, flush_context):
        nonlocal flushed
        if any(row.__tablename__ == "candidate_incident_convergences" for row in session.new):
            flushed = True
            raise RuntimeError("模拟事故收敛已经flush、尚未提交时崩溃")

    event.listen(Session, "after_flush", crash)
    try:
        with pytest.raises(RuntimeError, match="尚未提交"):
            await c.convergence.converge_next()
    finally:
        event.remove(Session, "after_flush", crash)
    assert flushed and await count(c) == 0 and await marker(c) is None
    assert (await snapshots(c), await c.incidents.get_incident(c.started.run.incident_id)) == before
    assert await c.incidents.list_events(c.started.run.incident_id) == events_before
    assert await c.convergence.converge_next()
    response = await get(c)
    assert response.status_code == 200, response.text
    assert await count(c) == 1 and await marker(c) is not None
    assert not await c.convergence.converge_next()
    assert (await get(c)).json() == response.json()
    assert await snapshots(c) == before[0]
    async with c.db() as session:
        digest = await session.scalar(
            text("SELECT content_sha256 FROM candidate_recovery_results WHERE job_id=:id"),
            {"id": identifier},
        )
    try:
        await replace(
            c,
            "UPDATE candidate_recovery_results SET content_sha256=:value WHERE job_id=:id",
            {"id": identifier, "value": "f" * 64},
        )
        assert (await get(c)).status_code == 409
        assert await count(c) == 1
    finally:
        await replace(
            c,
            "UPDATE candidate_recovery_results SET content_sha256=:value WHERE job_id=:id",
            {"id": identifier, "value": digest},
        )
    assert (await get(c)).json() == response.json()
