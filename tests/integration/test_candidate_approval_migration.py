"""真实 PostgreSQL 验证候选审批请求的来源隔离、可信命令和不可变闭环。"""

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4, uuid5

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.approval.candidate.contracts import RequestCommand
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.integration import test_candidate_plan_migration as plans
from tests.task_facts_helpers import BASE_TIME

pytestmark = pytest.mark.integration

ROUTE = (
    "/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan/approval-request"
)


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def encoded(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def canonical_command() -> dict:
    return {
        "action": "request_approval",
        "plan_content_sha256": "a" * 64,
        "expected_wait_version": None,
        "expected_incident_version": 2,
        "acknowledge_unresolved_conditions": True,
        "idempotency_key": "migration-candidate-plan",
        "reason": "工程师请求生成尚未授权的独立草案",
    }


async def authorize(session, values, *, invalid=None) -> None:
    permissions = ["request_approval", "manage_incident"]
    if invalid in permissions:
        permissions.remove(invalid)
    await session.execute(
        text(
            "INSERT INTO operator_authorizations (id,policy_version,subject_id,credential_id,"
            "role,permissions,operation,method,route,resources,body_sha256,authorized_at) "
            "VALUES (:authorization,'operator-auth-v1','migration-engineer','migration',:role,"
            "CAST(:permissions AS jsonb),:operation,'POST',:route,CAST(:resources AS jsonb),"
            ":body_hash,:now)"
        ),
        {
            **values,
            "role": "viewer" if invalid == "role" else "engineer",
            "permissions": encoded(permissions),
            "operation": "evaluate_investigation_candidate"
            if invalid == "operation"
            else "create_candidate_approval_request",
            "route": ROUTE + "/commands" if invalid == "route" else ROUTE,
            "resources": encoded(
                {
                    "run_id": str(uuid4() if invalid == "run" else values["run"]),
                    "job_id": str(uuid4() if invalid == "job" else values["job"]),
                }
            ),
            "body_hash": "b" * 64 if invalid == "body_hash" else digest(values["raw"]),
        },
    )


async def insert_audit(session, values, *, outcome="error") -> None:
    await session.execute(
        text(
            "INSERT INTO candidate_approval_commands (authorization_id,investigation_run_id,"
            "candidate_job_id,subject_id,command,raw_body,command_sha256,outcome,http_status,"
            "request_id,error_code,error_message,created_at) VALUES (:authorization,:run,:job,"
            ":subject,CAST(:command AS jsonb),:raw,:command_hash,:outcome,:status,:request,"
            ":error,:message,:now)"
        ),
        {
            **values,
            "outcome": outcome,
            "status": {"error": 404, "applied": 201, "replayed": 200}[outcome],
            "request": None if outcome == "error" else values["id"],
            "error": "candidate_approval_run_not_found" if outcome == "error" else None,
            "message": "目标运行不存在" if outcome == "error" else None,
        },
    )


async def insert_refusal(factory, *, invalid=None, raw=None, canonical=None) -> None:
    body = canonical_command() if canonical is None else canonical
    raw = encoded(body) if raw is None else raw if isinstance(raw, str) else encoded(raw)
    values = {
        "authorization": uuid4(),
        "run": uuid4(),
        "job": uuid4(),
        "subject": "another-engineer" if invalid == "subject" else "migration-engineer",
        "command": encoded(body),
        "command_hash": "c" * 64 if invalid == "command_hash" else digest(encoded(body)),
        "raw": raw,
        "now": BASE_TIME,
    }
    async with factory() as session, session.begin():
        await authorize(session, values, invalid=invalid)
        await insert_audit(session, values)


@pytest.mark.asyncio
async def test_0074_metadata_and_empty_roundtrip(database_session_factory) -> None:
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0073_candidate_plan_admission")
        async with database_session_factory() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0073_candidate_plan_admission"
            )
            for table in ("candidate_approval_requests", "candidate_approval_commands"):
                assert (
                    await session.scalar(text("SELECT to_regclass(:name)"), {"name": table}) is None
                )
        await asyncio.to_thread(command.upgrade, config(), "head")
        await asyncio.to_thread(command.check, config())
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


@pytest.mark.asyncio
async def test_0074_guards_cover_plan_and_command_without_legacy_columns(
    database_session_factory,
) -> None:
    async with database_session_factory() as session:
        guards = dict(
            (
                await session.execute(
                    text(
                        "SELECT tgname,tgdeferrable FROM pg_trigger WHERE "
                        "tgname IN ('candidate_approval_requests_guard',"
                        "'candidate_approval_commands_guard',"
                        "'candidate_approval_requests_state','candidate_approval_commands_state')"
                    )
                )
            ).all()
        )
        assert guards == {
            "candidate_approval_requests_guard": False,
            "candidate_approval_commands_guard": False,
            "candidate_approval_requests_state": True,
            "candidate_approval_commands_state": True,
        }
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name IN "
                    "('action_plans','approval_requests','operation_executions') "
                    "AND column_name LIKE '%candidate%'"
                )
            )
            == 0
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid",
    [
        "role",
        "operation",
        "route",
        "run",
        "job",
        "body_hash",
        "subject",
        "request_approval",
        "manage_incident",
    ],
)
async def test_0074_refusal_requires_precise_trusted_admission(
    database_session_factory, invalid
) -> None:
    with pytest.raises(DBAPIError, match="精确原始正文或同源可信准入"):
        await insert_refusal(database_session_factory, invalid=invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "field", ["reason", "idempotency_key", "plan_content_sha256", "expected_incident_version"]
)
async def test_0074_raw_body_cannot_authorize_a_different_canonical_command(
    database_session_factory, field
) -> None:
    original = canonical_command()
    altered = {
        **original,
        field: {
            "reason": "另一份合法但未经此次正文授权的理由",
            "idempotency_key": "another-valid-plan-key",
            "plan_content_sha256": "b" * 64,
            "expected_incident_version": 3,
        }[field],
    }
    RequestCommand.model_validate(original)
    RequestCommand.model_validate(altered)
    with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
        await insert_refusal(database_session_factory, raw=original, canonical=altered)


@pytest.mark.asyncio
@pytest.mark.parametrize("normalization", ["default_action", "ascii_trim", "unicode_trim"])
async def test_0074_raw_body_accepts_actual_pydantic_normalization(
    database_session_factory, normalization
) -> None:
    raw = canonical_command()
    if normalization == "default_action":
        raw.pop("action")
    else:
        padding = " \t\r\n" if normalization == "ascii_trim" else "\u3000\u00a0\u2003"
        for field in ("reason", "idempotency_key", "plan_content_sha256"):
            raw[field] = padding + raw[field] + padding
    normalized = RequestCommand.model_validate(raw).model_dump(mode="json")
    assert normalized == canonical_command()
    await insert_refusal(database_session_factory, raw=raw, canonical=normalized)


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["expected_wait_version", "expected_incident_version"])
@pytest.mark.parametrize("token", ["2.0", "2e0", '"2"', "true"])
async def test_0074_raw_versions_are_strict_json_integers(
    database_session_factory, field, token
) -> None:
    canonical = {**canonical_command(), "expected_wait_version": 2}
    raw = encoded(canonical).replace('"' + field + '":2', '"' + field + '":' + token)
    with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
        await insert_refusal(database_session_factory, raw=raw, canonical=canonical)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "change",
    [
        {"action": "execute"},
        {"plan_content_sha256": "invalid"},
        {"expected_wait_version": 1},
        {"expected_incident_version": 0},
        {"execution_eligible": True},
        {"acknowledge_unresolved_conditions": False},
        {"acknowledge_unresolved_conditions": "true"},
        {"acknowledge_unresolved_conditions": 1},
        {"reason": "\u3000"},
    ],
)
async def test_0074_native_command_rejects_invalid_contract(
    database_session_factory, change
) -> None:
    with pytest.raises(DBAPIError):
        await insert_refusal(database_session_factory, canonical={**canonical_command(), **change})


@pytest.mark.asyncio
async def test_0074_native_command_rejects_a_forged_canonical_hash(
    database_session_factory,
) -> None:
    with pytest.raises(DBAPIError, match="有界精确规范来源绑定"):
        await insert_refusal(database_session_factory, invalid="command_hash")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE candidate_approval_commands SET error_message='篡改'",
        "DELETE FROM candidate_approval_commands",
    ],
)
async def test_0074_refusal_is_immutable(database_session_factory, mutation) -> None:
    await insert_refusal(database_session_factory)
    with pytest.raises(DBAPIError, match="不可更新或删除"):
        async with database_session_factory() as session, session.begin():
            await session.execute(text(mutation))


@pytest.mark.asyncio
async def test_0074_downgrade_preserves_even_refusal_facts(database_session_factory) -> None:
    await insert_refusal(database_session_factory)
    with pytest.raises(Exception, match="不能删除已保存的候选审批事实"):
        await asyncio.to_thread(command.downgrade, config(), "0073_candidate_plan_admission")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0082_candidate_release_job"
        )
        assert await session.scalar(text("SELECT count(*) FROM candidate_approval_commands")) == 1


async def ready_request(c):
    plan = await plans.ready_plan(c)
    async with c.db() as session, session.begin():
        await plans.authorize(session, plan)
        await plans.insert_plan(session, plan)
        await plans.insert_audit(session, plan, outcome="applied")
        expires = await session.scalar(
            text(
                "SELECT started_at + make_interval(secs => "
                "(call_budget->>'max_duration_seconds')::int) FROM investigation_runs "
                "WHERE id=:run"
            ),
            plan,
        )
    body = {
        **canonical_command(),
        "plan_content_sha256": plan["content_hash"],
        "expected_wait_version": plan["wait"],
        "expected_incident_version": plan["incident"],
    }
    return {
        **plan,
        "id": uuid5(plan["id"], "candidate-approval-request:v1"),
        "plan": plan["id"],
        "authorization": uuid4(),
        "command": encoded(body),
        "command_hash": digest(encoded(body)),
        "raw": encoded(body),
        "key": body["idempotency_key"],
        "previous": plan["incident"],
        "incident": plan["incident"] + 1,
        "expires": expires,
        "incident_id": c.started.run.incident_id,
        "reason": body["reason"],
    }


async def transition(session, values, **changes):
    await session.execute(
        text(
            "UPDATE incidents SET status='awaiting_approval',version=:incident,updated_at=:now "
            "WHERE id=:incident_id"
        ),
        values,
    )
    return await session.scalar(
        text(
            "INSERT INTO incident_events (incident_id,event_type,actor_type,actor_id,"
            "from_status,to_status,reason,incident_version,payload,occurred_at) VALUES "
            "(:incident_id,'incident.status_changed',:actor_type,:subject,:from_status,"
            "'awaiting_approval',:reason,:incident,CAST(:payload AS jsonb),:now) RETURNING id"
        ),
        {
            "actor_type": "human",
            "from_status": "investigating",
            "payload": encoded({"action": "request_approval"}),
            **values,
            **changes,
        },
    )


async def insert_request(session, values):
    await session.execute(
        text(
            "INSERT INTO candidate_approval_requests (id,investigation_run_id,candidate_job_id,"
            "plan_id,plan_content_sha256,wait_version,previous_incident_version,incident_version,"
            "event_cursor,policy_version,status,requested_by_id,authorization_id,idempotency_key,"
            "command_sha256,created_at,expires_at) VALUES (:id,:run,:job,:plan,:content_hash,:wait,"
            ":previous,:incident,:event,'candidate-approval-v1','pending',:subject,:authorization,"
            ":key,:command_hash,:now,:expires)"
        ),
        values,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0074_native_request_atomic_success_and_immutable_event(wait_context):
    c = wait_context
    values = await ready_request(c)
    async with c.db() as session, session.begin():
        await authorize(session, values)
        values["event"] = await transition(session, values)
        await insert_request(session, values)
        await insert_audit(session, values, outcome="applied")
    async with c.db() as session:
        assert await session.scalar(text("SELECT count(*) FROM candidate_approval_requests")) == 1
        assert (
            await session.scalar(text("SELECT status FROM incidents WHERE id=:incident_id"), values)
            == "awaiting_approval"
        )
        for table in ("action_plans", "approval_requests", "operation_executions"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    for mutation in (
        "UPDATE candidate_approval_requests SET status='pending'",
        "DELETE FROM candidate_approval_requests",
        "UPDATE incident_events SET reason='篡改' WHERE id=:event",
        "DELETE FROM incident_events WHERE id=:event",
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with c.db() as session, session.begin():
                await session.execute(text(mutation), values)
    with pytest.raises(Exception, match="不能删除已保存的候选审批事实"):
        await asyncio.to_thread(command.downgrade, config(), "0073_candidate_plan_admission")


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0074_request_without_audit_rolls_back_incident(wait_context):
    c = wait_context
    values = await ready_request(c)
    with pytest.raises(DBAPIError, match="同事务精确成功命令闭环"):
        async with c.db() as session, session.begin():
            await authorize(session, values)
            values["event"] = await transition(session, values)
            await insert_request(session, values)
    async with c.db() as session:
        assert (
            await session.scalar(text("SELECT status FROM incidents WHERE id=:incident_id"), values)
            == "investigating"
        )
        assert await session.scalar(text("SELECT count(*) FROM candidate_approval_requests")) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize(
    "mutation",
    ["old_event", "reason", "actor", "action", "phase_version", "plan_version", "deadline"],
)
async def test_0074_request_cannot_forge_phase_event_or_extend_deadline(wait_context, mutation):
    c = wait_context
    values = await ready_request(c)
    changes = {
        "reason": {"reason": "不是此次请求的真实理由"},
        "actor": {"actor_type": "system"},
        "action": {"payload": encoded({"action": "approve_plan"})},
    }.get(mutation, {})
    if mutation == "phase_version":
        values["incident"] += 1
    elif mutation == "plan_version":
        values["previous"] += 1
        values["incident"] += 1
    elif mutation == "deadline":
        values["expires"] += timedelta(seconds=1)
    if mutation == "old_event":
        async with c.db() as session, session.begin():
            values["event"] = await transition(session, values)
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            await authorize(session, values)
            if mutation != "old_event":
                values["event"] = await transition(session, values, **changes)
            await insert_request(session, values)
            await insert_audit(session, values, outcome="applied")


@pytest.mark.asyncio
@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0074_native_transition_checks_actual_previous_state_and_version(wait_context):
    c = wait_context
    values = await ready_request(c)
    for status, version in (
        ("open", values["previous"]),
        ("investigating", values["previous"] + 3),
    ):
        async with c.db() as session, session.begin():
            await session.execute(
                text("UPDATE incidents SET status=:status,version=:version WHERE id=:incident_id"),
                {**values, "status": status, "version": version},
            )
        with pytest.raises(DBAPIError, match="必须从真实 investigating 状态及相邻版本开始"):
            async with c.db() as session, session.begin():
                await transition(session, values)
