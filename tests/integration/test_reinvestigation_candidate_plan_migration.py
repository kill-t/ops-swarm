"""回流候选草案的元数据一致性与空库迁移往返。"""

import asyncio
import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from tests.integration.test_candidate_approval_migration import config
from tests.unit.planning.test_reinvestigation_candidate_plan import command_data

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_0094_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0093_reflow_candidate")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT to_regclass('reinvestigation_candidate_plans')"))
                is None
            )
            assert (
                await session.scalar(
                    text("SELECT to_regclass('reinvestigation_candidate_plan_commands')")
                )
                is None
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
    await asyncio.to_thread(command.check, config())


async def test_native_command_keeps_strict_raw_versions(database_session_factory):
    body = command_data()
    canonical = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    async with database_session_factory() as session:
        assert (
            await session.scalar(
                text("SELECT normalize_reflow_plan_command(:raw)"), {"raw": canonical}
            )
            == body
        )
        for token in ("2.0", "2e0", '"2"', "true"):
            raw = canonical.replace(
                '"expected_incident_version":2', '"expected_incident_version":' + token
            )
            assert (
                await session.scalar(
                    text("SELECT normalize_reflow_plan_command(:raw)"), {"raw": raw}
                )
                is None
            )


async def insert_refusal(session, *, invalid=None, lifetime=timedelta(minutes=1)):
    body = command_data()
    raw = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    identity, authorization = uuid4(), uuid4()
    created_at = await session.scalar(text("SELECT clock_timestamp()"))
    permissions = ["plan_action", "manage_incident"]
    if invalid in permissions:
        permissions.remove(invalid)
    await session.execute(
        text(
            "INSERT INTO operator_authorizations(id,policy_version,subject_id,credential_id,role,"
            "permissions,operation,method,route,resources,body_sha256,authorized_at) VALUES "
            "(:auth,'operator-auth-v1','native-engineer','native-139',:role,"
            "CAST(:permissions AS jsonb),"
            ":operation,'POST',:route,CAST(:resources AS jsonb),:body_hash,:at)"
        ),
        {
            "auth": authorization,
            "role": "viewer" if invalid == "role" else "engineer",
            "permissions": json.dumps(permissions),
            "operation": "create_candidate_action_plan"
            if invalid == "operation"
            else "create_reinvestigation_candidate_plan",
            "route": "/invalid-route"
            if invalid == "route"
            else "/api/v1/investigations/{run_id}/reinvestigation-candidate/action-plan",
            "resources": json.dumps({"run_id": str(uuid4() if invalid == "run" else identity)}),
            "body_hash": "0" * 64
            if invalid == "body_hash"
            else hashlib.sha256(raw.encode()).hexdigest(),
            "at": created_at,
        },
    )
    await session.execute(
        text(
            "INSERT INTO reinvestigation_candidate_plan_commands(authorization_id,"
            "investigation_run_id,"
            "candidate_job_id,subject_id,command,raw_body,command_sha256,outcome,http_status,"
            "plan_id,error_code,error_message,created_at,credential_expires_at) VALUES "
            "(:auth,:run,:run,'native-engineer',CAST(:body AS jsonb),:raw,"
            "reflow_hash(CAST(:body AS jsonb)),'error',404,NULL,'reflow_plan_run_not_found',"
            "'原运行不存在',:at,:expires)"
        ),
        {
            "auth": authorization,
            "run": identity,
            "body": raw,
            "raw": raw,
            "at": created_at,
            "expires": created_at + lifetime,
        },
    )


async def test_native_refusal_requires_exact_trust_and_unexpired_commit(database_session_factory):
    async with database_session_factory() as session, session.begin():
        for invalid in (
            "role",
            "plan_action",
            "manage_incident",
            "operation",
            "route",
            "run",
            "body_hash",
        ):
            with pytest.raises(DBAPIError):
                async with session.begin_nested():
                    await insert_refusal(session, invalid=invalid)
    wrote_audit = False
    with pytest.raises(DBAPIError, match="提交前操作者凭据已过期"):
        async with database_session_factory() as session, session.begin():
            await insert_refusal(session, lifetime=timedelta(seconds=2))
            wrote_audit = True
            await session.execute(text("SELECT pg_sleep(2.1)"))
    assert wrote_audit, "拒绝审计必须先成功插入，由提交门禁重新检查有效期"
    async with database_session_factory() as session:
        assert (
            await session.scalar(
                text("SELECT count(*) FROM reinvestigation_candidate_plan_commands")
            )
            == 0
        )
