"""真实 PostgreSQL 核验人工等待迁移和数据库提交护栏。"""

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
from tests.integration.test_coordination_experience_migration import claim_v3, start_v3_run
from tests.task_facts_helpers import BASE_TIME

pytestmark = pytest.mark.integration


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def insert_error_audit(factory, *, invalid: str | None = None) -> None:
    """未知目标也保留可信业务拒绝；不伪造有效交接或人工事件。"""

    run_id, authorization_id = uuid4(), uuid4()
    body = {
        "action": "cancel",
        "handoff_id": str(uuid4()),
        "source_sha256": "a" * 64,
        "expected_wait_version": 1,
        "idempotency_key": "migration-command",
        "reason": "取消隔离演练调查",
    }
    raw_body = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    values = {
        "auth": authorization_id,
        "run": run_id,
        "subject": "engineer-test",
        "role": "viewer" if invalid == "role" else "engineer",
        "operation": "supplement_investigation_wait"
        if invalid == "operation"
        else "cancel_investigation_wait",
        "route": "/api/v1/investigations/{run_id}/human-wait/cancel",
        "resources": json.dumps({"run_id": str(uuid4() if invalid == "resources" else run_id)}),
        "hash": "b" * 64
        if invalid == "raw_hash"
        else hashlib.sha256(raw_body.encode()).hexdigest(),
        "now": BASE_TIME + timedelta(seconds=10),
        "body": json.dumps(body, ensure_ascii=False),
        "raw": raw_body,
        "command_hash": hashlib.sha256(raw_body.encode()).hexdigest(),
    }
    async with factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO operator_authorizations (id,policy_version,subject_id,"
                "credential_id,role,permissions,operation,method,route,resources,"
                "body_sha256,authorized_at) VALUES (:auth,'operator-auth-v1',:subject,"
                "'migration',:role,'[\"read\",\"manage_incident\"]'::jsonb,:operation,"
                "'POST',:route,CAST(:resources AS jsonb),:hash,:now)"
            ),
            values,
        )
        if invalid == "subject":
            values["subject"] = "another-engineer"
        if invalid == "payload":
            body["expected_wait_version"] = 99
            values["body"] = json.dumps(body)
        await session.execute(
            text(
                "INSERT INTO investigation_human_wait_commands (authorization_id,"
                "investigation_run_id,subject_id,command,raw_body,command_sha256,outcome,"
                "http_status,error_code,error_message,created_at) VALUES (:auth,:run,"
                ":subject,CAST(:body AS jsonb),:raw,:command_hash,'error',404,"
                "'investigation_run_not_found','目标运行不存在',:now)"
            ),
            values,
        )


@pytest.mark.asyncio
async def test_0068_metadata_and_empty_roundtrip(database_session_factory) -> None:
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0067_terminal_handoff")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "0067_terminal_handoff"
            )
            assert (
                await session.scalar(text("SELECT to_regclass('investigation_human_wait_events')"))
                is None
            )
            original = await session.scalar(
                text("SELECT pg_get_functiondef('validate_terminal_handoff_state()'::regprocedure)")
            )
            assert "investigation_human_wait_events" not in original
            assert "cancelled" not in original
        await asyncio.to_thread(command.upgrade, config(), "head")
        await asyncio.to_thread(command.check, config())
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


@pytest.mark.asyncio
async def test_0068_native_guards_and_deferred_source(database_session_factory) -> None:
    async with database_session_factory() as session:
        guards = dict(
            (
                await session.execute(
                    text(
                        "SELECT tgname,tgdeferrable FROM pg_trigger WHERE tgname IN "
                        "('human_wait_events_immutable','human_wait_commands_immutable',"
                        "'human_wait_events_source','human_wait_commands_source','human_wait_events_state')"
                    )
                )
            ).all()
        )
        assert guards == {
            "human_wait_events_immutable": False,
            "human_wait_commands_immutable": False,
            "human_wait_events_source": False,
            "human_wait_commands_source": False,
            "human_wait_events_state": True,
        }
        fk = (
            await session.execute(
                text(
                    "SELECT condeferrable,condeferred FROM pg_constraint "
                    "WHERE conname='fk_human_wait_events_command'"
                )
            )
        ).one()
        assert tuple(fk) == (True, True)
        constraints = dict(
            (
                await session.execute(
                    text(
                        "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname IN "
                        "('ck_investigation_runs_cancelled_scope','ck_task_events_type',"
                        "'uq_terminal_handoffs_id_run')"
                    )
                )
            ).all()
        )
        assert (
            "dynamic-investigation-graph-v15"
            in constraints["ck_investigation_runs_cancelled_scope"]
        )
        assert "run.human_input_saved" in constraints["ck_task_events_type"]
        assert "run.cancelled" in constraints["ck_task_events_type"]
        assert constraints["uq_terminal_handoffs_id_run"] == "UNIQUE (id, investigation_run_id)"


@pytest.mark.asyncio
async def test_0068_native_cancelled_run_requires_real_handoff(database_session_factory) -> None:
    store, started = await start_v3_run(database_session_factory, experience=False)
    await claim_v3(store)
    with pytest.raises(DBAPIError, match="缺少终端交接事实"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_runs SET status='cancelled',completed_at=:now "
                    "WHERE id=:run"
                ),
                {"run": started.run.id, "now": BASE_TIME + timedelta(seconds=10)},
            )


@pytest.mark.asyncio
async def test_0068_native_input_requires_real_handoff(database_session_factory) -> None:
    store, started = await start_v3_run(database_session_factory, experience=False)
    await claim_v3(store)
    with pytest.raises(DBAPIError, match="精确有效等待来源及连续版本"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_human_wait_events (id,investigation_run_id,"
                    "handoff_id,state_version,action,subject_id,authorization_id,idempotency_key,"
                    "command_sha256,created_at) VALUES (:id,:run,:handoff,2,'supplement',"
                    "'engineer-test',:auth,'migration-command',:hash,:now)"
                ),
                {
                    "id": uuid4(),
                    "run": started.run.id,
                    "handoff": uuid4(),
                    "auth": uuid4(),
                    "hash": "a" * 64,
                    "now": BASE_TIME + timedelta(seconds=10),
                },
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["role", "operation", "resources", "raw_hash", "subject", "payload"]
)
async def test_0068_native_audit_rejects_forged_admission(
    database_session_factory, invalid
) -> None:
    with pytest.raises(DBAPIError, match="人工等待命令缺少|人工等待规范命令"):
        await insert_error_audit(database_session_factory, invalid=invalid)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE investigation_human_wait_commands SET error_message='篡改'",
        "DELETE FROM investigation_human_wait_commands",
    ],
)
async def test_0068_unknown_run_audit_is_immutable(database_session_factory, mutation) -> None:
    await insert_error_audit(database_session_factory)
    with pytest.raises(DBAPIError, match="不可更新或删除"):
        async with database_session_factory() as session, session.begin():
            await session.execute(text(mutation))


@pytest.mark.asyncio
async def test_0068_downgrade_preserves_rejected_commands(database_session_factory) -> None:
    await insert_error_audit(database_session_factory)
    with pytest.raises(Exception, match="不能删除已保存的人工等待事实"):
        await asyncio.to_thread(command.downgrade, config(), "0067_terminal_handoff")
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0094_reflow_candidate_plan"
        )
        assert (
            await session.scalar(text("SELECT count(*) FROM investigation_human_wait_commands"))
            == 1
        )
