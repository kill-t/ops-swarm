"""真实 PostgreSQL 验证重评任务迁移与共享预算真实执行归属护栏。"""

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


async def insert_refusal(factory, *, invalid=None) -> None:
    run_id, auth_id, preparation_id = uuid4(), uuid4(), uuid4()
    body = {
        "action": "reassess",
        "handoff_id": str(uuid4()),
        "source_sha256": "a" * 64,
        "preparation_sha256": "c" * 64,
        "expected_wait_version": 1,
        "idempotency_key": "migration-prepare",
        "reason": "核对人工约束进入完整输入",
    }
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(raw.encode()).hexdigest()
    values = {
        "run": run_id,
        "preparation": preparation_id,
        "auth": auth_id,
        "subject": "migration-engineer",
        "role": "viewer" if invalid == "role" else "engineer",
        "operation": "cancel_investigation_wait"
        if invalid == "operation"
        else "reassess_investigation_human_input",
        "route": (
            "/api/v1/investigations/{run_id}/human-wait/preparations/{preparation_id}/reassessments"
        ),
        "resources": json.dumps(
            {
                "run_id": str(uuid4() if invalid == "resources" else run_id),
                "preparation_id": str(preparation_id),
            }
        ),
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
                "INSERT INTO investigation_human_review_commands (authorization_id,"
                "investigation_run_id,preparation_job_id,subject_id,command,raw_body,"
                "command_sha256,outcome,"
                "http_status,error_code,error_message,created_at) "
                "VALUES (:auth,:run,:preparation,:subject,"
                "CAST(:command AS jsonb),:raw,:command_hash,'error',404,"
                "'preparation_run_not_found','目标运行不存在',:now)"
            ),
            values,
        )


@pytest.mark.asyncio
async def test_0070_metadata_and_empty_roundtrip(database_session_factory) -> None:
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0069_human_input_preparation")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "0069_human_input_preparation"
            )
            assert (
                await session.scalar(text("SELECT to_regclass('investigation_human_review_jobs')"))
                is None
            )
            original = await session.scalar(
                text("SELECT pg_get_functiondef('validate_terminal_handoff_state()'::regprocedure)")
            )
            assert "investigation_human_wait_events" in original
            assert "investigation_human_review" not in original
        await asyncio.to_thread(command.upgrade, config(), "head")
        await asyncio.to_thread(command.check, config())
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


@pytest.mark.asyncio
async def test_0070_guards_cover_all_preparation_facts(database_session_factory) -> None:
    async with database_session_factory() as session:
        guards = dict(
            (
                await session.execute(
                    text(
                        "SELECT tgname,tgdeferrable FROM pg_trigger WHERE tgname IN "
                        "('human_review_jobs_guard','human_review_attempts_guard',"
                        "'human_review_results_guard','human_review_commands_guard',"
                        "'human_review_jobs_state','human_review_attempts_state',"
                        "'human_review_results_state')"
                    )
                )
            ).all()
        )
        assert guards == {
            "human_review_jobs_guard": False,
            "human_review_attempts_guard": False,
            "human_review_results_guard": False,
            "human_review_commands_guard": False,
            "human_review_jobs_state": True,
            "human_review_attempts_state": True,
            "human_review_results_state": True,
        }
        constraints = dict(
            (
                await session.execute(
                    text(
                        "SELECT conname,pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname IN "
                        "('fk_call_reservations_human_attempt','ck_call_reservations_human_owner',"
                        "'ck_call_reservations_invocation_key','ck_call_reservations_operation')"
                    )
                )
            ).all()
        )
        assert (
            "human_review_attempt_id, investigation_run_id"
            in constraints["fk_call_reservations_human_attempt"]
        )
        assert "human_review" in constraints["ck_call_reservations_human_owner"]
        assert "human-review:" in constraints["ck_call_reservations_invocation_key"]
        assert "human_review" in constraints["ck_call_reservations_operation"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid", ["role", "operation", "resources", "body_hash", "subject", "action"]
)
async def test_0070_refusal_audit_rejects_forged_admission(
    database_session_factory, invalid
) -> None:
    with pytest.raises(DBAPIError, match="重评命令缺少"):
        await insert_refusal(database_session_factory, invalid=invalid)


@pytest.mark.asyncio
async def test_0070_job_requires_actual_successful_preparation(database_session_factory) -> None:
    _, started = await start_v3_run(database_session_factory, experience=False)
    with pytest.raises(DBAPIError, match="精确成功准备工件"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_human_review_jobs (id,investigation_run_id,"
                    "preparation_job_id,preparation_sha256,wait_version,policy_version,"
                    "requested_by_id,authorization_id,idempotency_key,command_sha256,status,"
                    "attempt_count,available_at,created_at,updated_at) VALUES (:id,:run,:prep,"
                    ":hash,2,'human-input-reassessment-v1','migration-engineer',:auth,"
                    "'migration-reassess',:hash,'queued',0,:now,:now,:now)"
                ),
                {
                    "id": uuid4(),
                    "run": started.run.id,
                    "prep": uuid4(),
                    "auth": uuid4(),
                    "hash": "a" * 64,
                    "now": BASE_TIME + timedelta(seconds=5),
                },
            )


@pytest.mark.asyncio
async def test_0070_attempt_cannot_start_as_success(database_session_factory) -> None:
    with pytest.raises(DBAPIError, match="必须从运行状态开始"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_human_review_attempts "
                    "(id,job_id,investigation_run_id,number,"
                    "lease_token,worker_id,status,started_at,lease_expires_at,finished_at) "
                    "VALUES (:id,:job,:run,1,:token,'migration-worker','succeeded',:now,:end,:now)"
                ),
                {
                    "id": uuid4(),
                    "job": uuid4(),
                    "run": uuid4(),
                    "token": uuid4(),
                    "now": BASE_TIME,
                    "end": BASE_TIME + timedelta(seconds=30),
                },
            )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE investigation_human_review_commands SET error_message='篡改'",
        "DELETE FROM investigation_human_review_commands",
    ],
)
async def test_0070_refusal_audit_is_immutable(database_session_factory, mutation) -> None:
    await insert_refusal(database_session_factory)
    with pytest.raises(DBAPIError, match="不可更新或删除"):
        async with database_session_factory() as session, session.begin():
            await session.execute(text(mutation))


@pytest.mark.asyncio
async def test_0070_downgrade_preserves_refused_commands(database_session_factory) -> None:
    await insert_refusal(database_session_factory)
    with pytest.raises(Exception, match="不能删除已保存的人工重评事实"):
        await asyncio.to_thread(command.downgrade, config(), "0069_human_input_preparation")
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0080_candidate_release_request"
        )
        assert (
            await session.scalar(text("SELECT count(*) FROM investigation_human_review_commands"))
            == 1
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("human_operation", [False, True])
async def test_0070_ledger_rejects_forged_stage_ownership(
    database_session_factory, human_operation
) -> None:
    store, started = await start_v3_run(database_session_factory, experience=False)
    claimed = await claim_v3(store)
    operation = "human_review" if human_operation else "synthesize"
    key = (
        "human-review:" + str(uuid4())
        if human_operation
        else f"{claimed.attempt.id}:root:synthesize"
    )
    expected = "真实执行归属" if human_operation else "ck_call_reservations_human_owner"
    with pytest.raises(DBAPIError, match=expected):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_call_reservations (id,investigation_run_id,"
                    "investigation_attempt_id,human_review_attempt_id,sequence,idempotency_key,"
                    "operation,kind,status,reserved_at) VALUES (:id,:run,:parent,:owner,1,:key,"
                    ":operation,'model','reserved',:now)"
                ),
                {
                    "id": uuid4(),
                    "run": started.run.id,
                    "parent": claimed.attempt.id,
                    "owner": uuid4(),
                    "key": key,
                    "operation": operation,
                    "now": BASE_TIME + timedelta(seconds=5),
                },
            )
