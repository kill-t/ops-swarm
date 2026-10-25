"""真实批准来源下的只读执行前检查、可信命令及不可变历史。"""

import asyncio
import hashlib
import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from tests.api.test_candidate_approval_api import get as get_request
from tests.api.test_candidate_approval_api import incident_events
from tests.api.test_candidate_decision_api import decide, negative_command, parent_facts
from tests.api.test_candidate_decision_api import get as get_decision
from tests.api.test_candidate_decision_api import ready as ready_decision
from tests.api.test_candidate_plan_api import get as get_plan
from tests.api.test_human_wait_api import command, post
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.identity_helpers import auth_header, token

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class RecordingObserver:
    """仅记录受检步骤，不访问演练服务；可推进时间验证调用后门禁。"""

    def __init__(self, context):
        self.context = context
        self.steps = []
        self.advance_to = None

    async def observe(self, step):
        from opsswarm.execution.candidate.contracts import RuntimeObservation

        self.steps.append(step)
        observed_at = self.context.clock()
        if self.advance_to is not None:
            self.context.clock.current = self.advance_to
        return RuntimeObservation(
            action_type=step.action_type,
            observed_at=observed_at,
            source_path="/__admin/faults/release-500",
            release_enabled=True,
        )


async def ready(c, *, kind="terminal_handoff", approved=True):
    from opsswarm.execution.candidate.store import CandidatePreflightStore

    await ready_decision(c, kind=kind)
    c.observer = RecordingObserver(c)
    c.app.state.candidate_preflight_store = CandidatePreflightStore(
        c.db, observer=c.observer, clock=c.clock
    )
    c.preflight_base = f"{c.candidate_base}/{c.job_id}/execution-preflights"
    c.decision_record = None
    if approved:
        response = await decide(c)
        assert response.status_code == 201, response.text
        c.decision_record = response.json()["decision"]["record"]
    c.preflight_body = dict(
        decision_id=c.decision_record["id"] if approved else str(uuid4()),
        decision_content_sha256=c.decision_record["content_sha256"] if approved else "0" * 64,
        expected_incident_version=(
            c.decision_record["incident_version"]
            if approved
            else c.request_record["incident_version"]
        ),
        idempotency_key="candidate-preflight-command-120",
        reason="执行者核查原批准和当前演练参数，不解除未决条件",
    )


async def create(c, body=None, *, header=None):
    return await c.client.post(
        c.preflight_base,
        json=c.preflight_body if body is None else body,
        headers=header or auth_header("executor"),
    )


async def get(c, suffix=""):
    return await c.client.get(c.preflight_base + suffix, headers=auth_header("viewer"))


async def fact_counts(c):
    async with c.db() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in ("candidate_execution_preflights", "candidate_preflight_commands")
            ]
        )


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_preserve_unverified_conditions_parent_and_old_execution_facts(
    wait_context, kind
):
    c = wait_context
    await ready(c, kind=kind)
    before_parent, before_events = await parent_facts(c), await incident_events(c)
    assert (await get(c)).json() == {"items": [], "next_cursor": None}
    response = await create(c)
    assert response.status_code == 201, response.text
    assert response.json()["applied"] is True
    record = response.json()["record"]
    content = record["content"]
    operation_id = uuid5(UUID(c.decision_record["id"]), "candidate-operation:v1")
    assert record["operation_id"] == str(operation_id)
    assert record["id"] == str(
        uuid5(
            operation_id,
            hashlib.sha256(
                json.dumps(
                    {
                        "subject_id": "trusted-executor",
                        "idempotency_key": c.preflight_body["idempotency_key"],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            ).hexdigest(),
        )
    )
    assert record["investigation_run_id"] == str(c.started.run.id)
    assert record["candidate_job_id"] == str(c.job_id)
    assert record["decision_id"] == c.decision_record["id"]
    assert record["decision_content_sha256"] == c.decision_record["content_sha256"]
    assert record["incident_version"] == c.decision_record["incident_version"]
    assert record["requested_by_id"] == "trusted-executor"
    assert content["status"] == "blocked" and content["execution_eligible"] is False
    assert content["step"] == c.plan_record["content"]["plan"]["steps"][0]
    for name in ("preconditions", "unresolved_questions"):
        assert content[name] == c.plan_record["content"][name]
    assert content["blockers"] == [
        "unverified_preconditions",
        "unresolved_questions",
        "runtime_version_unverifiable",
    ]
    assert content["observation"]["release_enabled"] is True
    assert content["observation"]["source_path"] == "/__admin/faults/release-500"
    assert datetime.fromisoformat(record["expires_at"]) == min(
        datetime.fromisoformat(c.request_record["expires_at"]), c.clock() + timedelta(seconds=30)
    )
    assert len(c.observer.steps) == 1
    assert (
        record["content_sha256"]
        == hashlib.sha256(
            json.dumps(
                content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
            ).encode("utf-8")
        ).hexdigest()
    )
    assert (await get(c)).json()["items"] == [record]
    assert (await get_plan(c)).json()["record"] == c.plan_record
    assert (await get_request(c)).json()["record"] == c.request_record
    assert (await get_decision(c)).json()["record"] == c.decision_record
    assert c.plan_record["content"]["approval_eligible"] is False
    assert c.plan_record["content"]["execution_eligible"] is False
    assert await parent_facts(c) == before_parent
    assert len(await incident_events(c)) == len(before_events)
    async with c.db() as session:
        incident = await session.get(IncidentRow, c.started.run.incident_id)
        assert incident.status == "mitigating" and incident.version == record["incident_version"]
        admission = await session.get(OperatorAuthorizationRow, UUID(record["authorization_id"]))
        assert admission.subject_id == "trusted-executor" and admission.role == "executor"
        assert admission.operation == "check_candidate_execution"
        assert admission.resources == {"run_id": str(c.started.run.id), "job_id": str(c.job_id)}
        for table in ("action_plans", "action_steps", "approval_requests", "operation_executions"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_replay_and_reads_keep_observation_while_new_key_collects_new_history(wait_context):
    c = wait_context
    await ready(c)
    first = await create(c)
    assert first.status_code == 201, first.text
    record = first.json()["record"]
    c.clock.current += timedelta(seconds=1)
    replay = await create(c)
    assert replay.status_code == 200 and replay.json() == {"applied": False, "record": record}
    assert (await get(c)).json()["items"] == [record]
    assert len(c.observer.steps) == 1
    second = await create(c, {**c.preflight_body, "idempotency_key": "another-preflight-key-120"})
    assert second.status_code == 201, second.text
    newer = second.json()["record"]
    assert newer["id"] != record["id"] and newer["operation_id"] == record["operation_id"]
    assert (
        newer["content"]["observation"]["observed_at"]
        != record["content"]["observation"]["observed_at"]
    )
    page = await get(c, "?limit=1")
    assert page.status_code == 200 and page.json()["next_cursor"] is not None
    rest = await get(c, f"?after_cursor={page.json()['next_cursor']}")
    assert page.json()["items"] + rest.json()["items"] == [record, newer]
    c.clock.current = datetime.fromisoformat(c.request_record["expires_at"])
    assert (await get(c)).json()["items"] == [record, newer]
    assert (await create(c)).json() == {"applied": False, "record": record}
    assert len(c.observer.steps) == 2
    refused = await create(c, {**c.preflight_body, "idempotency_key": "expired-new-key-120"})
    assert refused.status_code == 409, refused.text
    assert len(c.observer.steps) == 2


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_permissions_strict_body_and_legacy_mode_require_trusted_executor(wait_context):
    c = wait_context
    await ready(c)
    for path in (c.preflight_base, c.preflight_base + "/commands"):
        assert (await c.client.get(path)).status_code == 401
        assert (await c.client.get(path, headers=auth_header("alert_source"))).status_code == 403
    assert (await c.client.post(c.preflight_base, json=c.preflight_body)).status_code == 401
    for role in ("viewer", "engineer", "approver", "alert_source"):
        assert (await create(c, header=auth_header(role))).status_code == 403
    for change in (
        {"actor_id": "forged-executor"},
        {"authorization_id": str(uuid4())},
        {"current_version": "v2"},
        {"conditions_satisfied": True},
        {"url": "http://localhost"},
        {"execution_eligible": True},
        {"expected_incident_version": True},
        {"expected_incident_version": "3"},
        {"decision_content_sha256": "invalid"},
        {"reason": " \n\t\u3000"},
        {"reason": "长" * 501},
    ):
        response = await create(c, {**c.preflight_body, **change})
        assert response.status_code == 422, response.text
    for field in c.preflight_body:
        body = dict(c.preflight_body)
        body.pop(field)
        assert (await create(c, body)).status_code == 422
    large = await c.client.post(
        c.preflight_base,
        content=json.dumps(c.preflight_body) + " " * 65536,
        headers={**auth_header("executor"), "Content-Type": "application/json"},
    )
    assert large.status_code == 413
    c.app.state.operator_auth_settings = c.settings.model_copy(update={"mode": "legacy_local"})
    assert (await c.client.post(c.preflight_base, json=c.preflight_body)).status_code == 401
    assert await fact_counts(c) == (0, 0) and c.observer.steps == []


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_wrong_decision_hash_version_and_resource_are_audited_before_observation(
    wait_context,
):
    c = wait_context
    await ready(c)
    for index, change in enumerate(
        (
            {"decision_id": str(uuid4())},
            {"decision_content_sha256": "f" * 64},
            {"expected_incident_version": c.decision_record["incident_version"] + 1},
        )
    ):
        response = await create(
            c, {**c.preflight_body, "idempotency_key": f"wrong-source-{index}-120", **change}
        )
        assert response.status_code == 409, response.text
    missing = await c.client.post(
        c.preflight_base.replace(str(c.job_id), str(uuid4())),
        json=c.preflight_body,
        headers=auth_header("executor"),
    )
    assert missing.status_code == 404, missing.text
    assert c.observer.steps == [] and await fact_counts(c) == (0, 4)
    audits = await get(c, "/commands")
    assert audits.status_code == 200 and len(audits.json()["items"]) == 3
    assert all(item["outcome"] == "error" for item in audits.json()["items"])
    assert "raw_body" not in audits.text
    assert (await create(c)).status_code == 201


@pytest.mark.parametrize("wait_context", ["human_clean_with_change"], indirect=True)
@pytest.mark.parametrize("change", ["pending", "rejected", "cancelled"])
async def test_missing_or_negative_decision_and_cancelled_source_cannot_be_observed(
    wait_context, change
):
    c = wait_context
    await ready(c, kind="human_route", approved=False)
    if change == "rejected":
        result = await decide(c, negative_command(c, "reject"))
        assert result.status_code == 201, result.text
        record = result.json()["decision"]["record"]
        c.preflight_body.update(
            decision_id=record["id"],
            decision_content_sha256=record["content_sha256"],
            expected_incident_version=record["incident_version"],
        )
    elif change == "cancelled":
        cancelled = await post(
            c, command(c, action="cancel", version=2, key="preflight-source-cancel-120")
        )
        assert cancelled.status_code == 201, cancelled.text
        assert (await decide(c)).status_code == 409
    response = await create(c)
    assert response.status_code in (404, 409), response.text
    assert await fact_counts(c) == (0, 1) and c.observer.steps == []


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("change", ["deadline", "incident"])
async def test_expired_or_changed_incident_refuses_new_observation(wait_context, change):
    c = wait_context
    await ready(c)
    if change == "deadline":
        c.clock.current = datetime.fromisoformat(c.request_record["expires_at"])
    else:
        async with c.db() as session, session.begin():
            await session.execute(
                text("UPDATE incidents SET version=version+1 WHERE id=:id"),
                {"id": c.started.run.incident_id},
            )
    response = await create(c)
    assert response.status_code == 409, response.text
    assert await fact_counts(c) == (0, 1) and c.observer.steps == []


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("change", ["deadline", "auth"])
async def test_observation_crossing_deadline_or_auth_rolls_back_preflight_and_applied_audit(
    wait_context, change
):
    c = wait_context
    await ready(c)
    before_parent, before_events = await parent_facts(c), await incident_events(c)
    c.observer.advance_to = (
        datetime.fromisoformat(c.request_record["expires_at"])
        if change == "deadline"
        else c.clock() + timedelta(days=2)
    )
    response = await create(c)
    assert response.status_code == (409 if change == "deadline" else 401), response.text
    assert len(c.observer.steps) == 1
    assert await fact_counts(c) == (0, 1 if change == "deadline" else 0)
    assert await parent_facts(c) == before_parent
    assert len(await incident_events(c)) == len(before_events)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("change", ["deadline", "auth"])
async def test_write_side_deadline_and_auth_rechecks_roll_back_check_and_applied_command(
    wait_context, change
):
    c = wait_context
    await ready(c)
    before_parent, before_events = await parent_facts(c), await incident_events(c)
    advanced = False

    def advance_after_flush(session, flush_context):
        nonlocal advanced
        if not advanced and any(
            row.__tablename__ == "candidate_preflight_commands" and row.outcome == "applied"
            for row in session.new
        ):
            advanced = True
            c.clock.current = (
                datetime.fromisoformat(c.request_record["expires_at"])
                if change == "deadline"
                else c.clock() + timedelta(days=2)
            )

    event.listen(Session, "after_flush", advance_after_flush)
    try:
        response = await create(c)
    finally:
        event.remove(Session, "after_flush", advance_after_flush)
    assert advanced and response.status_code == (409 if change == "deadline" else 401), (
        response.text
    )
    assert len(c.observer.steps) == 1 and await fact_counts(c) == (0, 0)
    assert await parent_facts(c) == before_parent
    assert len(await incident_events(c)) == len(before_events)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_concurrent_exact_replay_observes_once_and_full_command_conflicts_are_audited(
    wait_context,
):
    c = wait_context
    await ready(c)
    responses = await asyncio.gather(create(c), create(c))
    assert sorted(response.status_code for response in responses) == [200, 201]
    assert responses[0].json()["record"] == responses[1].json()["record"]
    assert len(c.observer.steps) == 1
    refused = await create(c, {**c.preflight_body, "reason": "同键不允许改变完整命令"})
    assert refused.status_code == 409, refused.text
    executor = next(item for item in c.settings.credentials if item.credential_id == "executor")
    c.app.state.operator_auth_settings = c.settings.model_copy(
        update={
            "credentials": (
                *c.settings.credentials,
                executor.model_copy(
                    update={"credential_id": "executor-two", "subject_id": "second-executor"}
                ),
            )
        }
    )
    header = {"Authorization": f"Bearer executor-two.{token('executor').split('.')[1]}"}
    other = await create(c, header=header)
    assert other.status_code == 201, other.text
    assert other.json()["record"]["id"] != responses[0].json()["record"]["id"]
    assert other.json()["record"]["operation_id"] == responses[0].json()["record"]["operation_id"]
    assert len(c.observer.steps) == 2 and await fact_counts(c) == (2, 4)
    page = await get(c, "/commands?limit=2")
    assert page.status_code == 200 and page.json()["next_cursor"] is not None
    rest = await get(c, f"/commands?after_cursor={page.json()['next_cursor']}")
    outcomes = [item["outcome"] for item in page.json()["items"] + rest.json()["items"]]
    assert sorted(outcomes) == ["applied", "applied", "error", "replayed"]


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_corrupted_preflight_or_original_source_cannot_be_read_or_replayed(wait_context):
    c = wait_context
    await ready(c)
    response = await create(c)
    assert response.status_code == 201, response.text
    record = response.json()["record"]
    async with c.db() as session:
        original_source_hash = await session.scalar(
            text(
                "SELECT source_content_sha256 FROM investigation_candidate_evaluation_jobs "
                "WHERE id=:id"
            ),
            {"id": c.job_id},
        )

    async def replace(sql, values):
        # 仅隔离测试库模拟已落盘损坏；业务命令不能绕过不可变守卫。
        async with c.db() as session, session.begin():
            await session.execute(text("SET LOCAL session_replication_role = replica"))
            await session.execute(text(sql), values)
            await session.execute(text("SET LOCAL session_replication_role = origin"))

    broken_content = {**record["content"], "blockers": []}
    attacks = (
        (
            "UPDATE candidate_execution_preflights SET content=CAST(:value AS jsonb) WHERE id=:id",
            UUID(record["id"]),
            json.dumps(broken_content, ensure_ascii=False),
            json.dumps(record["content"], ensure_ascii=False),
        ),
        (
            "UPDATE investigation_candidate_evaluation_jobs SET source_content_sha256=:value "
            "WHERE id=:id",
            c.job_id,
            "f" * 64,
            original_source_hash,
        ),
    )
    for sql, identifier, corrupt, original in attacks:
        try:
            await replace(sql, {"id": identifier, "value": corrupt})
            for result in (await get(c), await get(c, "/commands"), await create(c)):
                assert result.status_code == 409, result.text
            assert await fact_counts(c) == (1, 1)
            assert len(c.observer.steps) == 1
        finally:
            await replace(sql, {"id": identifier, "value": original})
    assert (await get(c)).json()["items"] == [record]
    assert (await get(c, "/commands")).status_code == 200
    replay = await create(c)
    assert replay.status_code == 200 and replay.json() == {"applied": False, "record": record}
    assert await fact_counts(c) == (1, 2) and len(c.observer.steps) == 1

    async def unavailable(step):
        from opsswarm.execution.candidate.contracts import RuntimeObservation

        c.observer.steps.append(step)
        return RuntimeObservation(
            action_type=step.action_type,
            observed_at=c.clock(),
            source_path="/__admin/faults/release-500",
            error_code="fault_lab_unavailable",
        )

    c.observer.observe = unavailable
    failed = await create(c, {**c.preflight_body, "idempotency_key": "failed-observation-120"})
    assert failed.status_code == 201, failed.text
    failed_record = failed.json()["record"]
    observation = failed_record["content"]["observation"]
    assert observation["error_code"] == "fault_lab_unavailable"
    assert observation["release_enabled"] is None
    assert observation["current_value"] is None and observation["available"] is None
    assert "runtime_observation_failed" in failed_record["content"]["blockers"]
    assert failed_record["content"]["execution_eligible"] is False
    assert (await get(c)).json()["items"] == [record, failed_record]
    replay = await create(c)
    assert replay.status_code == 200 and replay.json() == {"applied": False, "record": record}
    assert await fact_counts(c) == (2, 4) and len(c.observer.steps) == 2
