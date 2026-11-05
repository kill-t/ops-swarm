"""真实 PostgreSQL 验证准备任务正式迁移与原生事实护栏。"""

import asyncio
import hashlib
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from tests.integration.test_coordination_experience_migration import start_v3_run
from tests.task_facts_helpers import BASE_TIME

pytestmark = pytest.mark.integration


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def insert_refusal(factory, *, invalid=None) -> None:
    run_id, auth_id = uuid4(), uuid4()
    body = {
        "action": "prepare",
        "handoff_id": str(uuid4()),
        "source_sha256": "a" * 64,
        "expected_wait_version": 1,
        "idempotency_key": "migration-prepare",
        "reason": "核对人工约束进入完整输入",
    }
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    values = {
        "run": run_id,
        "auth": auth_id,
        "subject": "migration-engineer",
        "role": "viewer" if invalid == "role" else "engineer",
        "operation": "cancel_investigation_wait"
        if invalid == "operation"
        else "prepare_investigation_human_input",
        "route": "/api/v1/investigations/{run_id}/human-wait/preparations",
        "resources": json.dumps({"run_id": str(uuid4() if invalid == "resources" else run_id)}),
        "body_hash": "b" * 64 if invalid == "body_hash" else digest,
        "raw": raw,
        "command_hash": digest,
        "command": json.dumps(body, ensure_ascii=False),
        "now": BASE_TIME,
    }
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO operator_authorizations (id,policy_version,subject_id,credential_id,"
                "role,permissions,operation,method,route,resources,body_sha256,authorized_at) "
                "VALUES (:auth,'operator-auth-v1',:subject,'migration',:role,"
                "'[\"read\",\"manage_incident\"]'::jsonb,:operation,'POST',:route,"
                "CAST(:resources AS jsonb),:body_hash,:now)"
            ),
            values,
        )
        if invalid == "subject":
            values["subject"] = "another-engineer"
        if invalid == "action":
            body["action"] = "resume"
            values["command"] = json.dumps(body)
        await session.execute(
            text(
                "INSERT INTO investigation_human_preparation_commands (authorization_id,"
                "investigation_run_id,subject_id,command,raw_body,command_sha256,outcome,"
                "http_status,error_code,error_message,created_at) VALUES (:auth,:run,:subject,"
                "CAST(:command AS jsonb),:raw,:command_hash,'error',404,"
                "'preparation_run_not_found','目标运行不存在',:now)"
            ),
            values,
        )


@pytest.mark.asyncio
async def test_0069_metadata_and_empty_roundtrip(database_session_factory) -> None:
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0068_human_wait_commands")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "0068_human_wait_commands"
            )
            assert (
                await session.scalar(
                    text("SELECT to_regclass('investigation_human_preparation_jobs')")
                )
                is None
            )
            original = await session.scalar(
                text("SELECT pg_get_functiondef('validate_terminal_handoff_state()'::regprocedure)")
            )
            assert "investigation_human_wait_events" in original
            assert "investigation_human_preparation" not in original
        await asyncio.to_thread(command.upgrade, config(), "head")
        await asyncio.to_thread(command.check, config())
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


@pytest.mark.asyncio
async def test_0069_guards_cover_all_preparation_facts(database_session_factory) -> None:
    async with database_session_factory() as session:
        guards = dict(
            (
                await session.execute(
                    text(
                        "SELECT tgname,tgdeferrable FROM pg_trigger WHERE tgname IN "
                        "('human_preparation_jobs_guard','human_preparation_attempts_guard',"
                        "'human_preparation_artifacts_guard','human_preparation_commands_guard',"
                        "'human_preparation_jobs_state','human_preparation_attempts_state',"
                        "'human_preparation_artifacts_state')"
                    )
                )
            ).all()
        )
        assert guards == {
            "human_preparation_jobs_guard": False,
            "human_preparation_attempts_guard": False,
            "human_preparation_artifacts_guard": False,
            "human_preparation_commands_guard": False,
            "human_preparation_jobs_state": True,
            "human_preparation_attempts_state": True,
            "human_preparation_artifacts_state": True,
        }
        constraints = dict(
            (
                await session.execute(
                    text(
                        "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint WHERE "
                        "conname IN "
                        "('fk_human_preparation_jobs_handoff','fk_human_preparation_artifacts_attempt',"
                        "'uq_human_preparation_jobs_version')"
                    )
                )
            ).all()
        )
        assert (
            "handoff_id, investigation_run_id" in constraints["fk_human_preparation_jobs_handoff"]
        )
        assert "attempt_id, job_id" in constraints["fk_human_preparation_artifacts_attempt"]
        assert (
            constraints["uq_human_preparation_jobs_version"]
            == "UNIQUE (investigation_run_id, wait_version)"
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["role", "operation", "resources", "body_hash", "subject", "action"]
)
async def test_0069_refusal_audit_rejects_forged_admission(
    database_session_factory, invalid
) -> None:
    with pytest.raises(DBAPIError, match="准备命令缺少"):
        await insert_refusal(database_session_factory, invalid=invalid)


@pytest.mark.asyncio
async def test_0069_job_requires_actual_human_supplements(database_session_factory) -> None:
    _, started = await start_v3_run(database_session_factory, experience=False)
    with pytest.raises(DBAPIError, match="当前未取消的精确人工补充等待"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_human_preparation_jobs (id,investigation_run_id,"
                    "handoff_id,wait_version,policy_version,source,source_sha256,requested_by_id,"
                    "authorization_id,idempotency_key,command_sha256,status,attempt_count,"
                    "available_at,created_at,updated_at) VALUES (:id,:run,:handoff,2,"
                    "'human-input-preparation-v1','{}'::jsonb,:hash,'migration-engineer',:auth,"
                    "'migration-prepare',:hash,'queued',0,:now,:now,:now)"
                ),
                {
                    "id": uuid4(),
                    "run": started.run.id,
                    "handoff": uuid4(),
                    "auth": uuid4(),
                    "hash": "a" * 64,
                    "now": BASE_TIME + timedelta(seconds=5),
                },
            )


@pytest.mark.asyncio
async def test_0069_attempt_cannot_start_as_success(database_session_factory) -> None:
    with pytest.raises(DBAPIError, match="必须从运行状态开始"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_human_preparation_attempts (id,job_id,number,"
                    "lease_token,worker_id,status,started_at,lease_expires_at,finished_at) "
                    "VALUES (:id,:job,1,:token,'migration-worker','succeeded',:now,:end,:now)"
                ),
                {
                    "id": uuid4(),
                    "job": uuid4(),
                    "token": uuid4(),
                    "now": BASE_TIME,
                    "end": BASE_TIME + timedelta(seconds=30),
                },
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE investigation_human_preparation_commands SET error_message='篡改'",
        "DELETE FROM investigation_human_preparation_commands",
    ],
)
async def test_0069_refusal_audit_is_immutable(database_session_factory, mutation) -> None:
    await insert_refusal(database_session_factory)
    with pytest.raises(DBAPIError, match="不可更新或删除"):
        async with database_session_factory() as session, session.begin():
            await session.execute(text(mutation))


@pytest.mark.asyncio
async def test_0069_downgrade_preserves_refused_commands(database_session_factory) -> None:
    await insert_refusal(database_session_factory)
    with pytest.raises(Exception, match="不能删除已保存的人工输入准备事实"):
        await asyncio.to_thread(command.downgrade, config(), "0068_human_wait_commands")
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0097_live_dynamic_investigation"
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM investigation_human_preparation_commands")
            )
            == 1
        )
