"""真实 PostgreSQL 核对授权审计表、旧事实兼容和降级保护。"""

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.identity.authentication import authenticate_operator
from opsswarm.identity.store import OperatorAuthorizationStore
from tests.api.test_control_plane_api import BASE_TIME
from tests.identity_helpers import auth_header, auth_settings
from tests.task_facts_helpers import started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def config():
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def test_0061_empty_audit_can_downgrade_without_rewriting_old_facts(database_session_factory):
    _, started = await started_run(database_session_factory)
    async with database_session_factory() as session:
        before = await session.scalar(
            text("SELECT to_jsonb(r) FROM investigation_runs r WHERE id = :id"),
            {"id": started.run.id},
        )
    # 0066 新增列在降级时被移除，单独核对默认值后比较全部旧列。
    assert before.pop("coordination_experience_policy_version") is None
    await asyncio.to_thread(command.downgrade, config(), "0060_versioned_runbook_read")
    try:
        async with database_session_factory() as session:
            after = await session.scalar(
                text("SELECT to_jsonb(r) FROM investigation_runs r WHERE id = :id"),
                {"id": started.run.id},
            )
            assert (
                await session.scalar(text("SELECT to_regclass('operator_authorizations')")) is None
            )
        assert after == before
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


async def test_0061_preserves_authorization_and_blocks_destructive_downgrade(
    database_session_factory,
):
    principal = authenticate_operator(
        auth_settings(), auth_header("engineer")["Authorization"], BASE_TIME
    )
    authorization_id = await OperatorAuthorizationStore(database_session_factory).record(
        principal,
        operation="start_investigation",
        method="POST",
        route="/api/v1/incidents/{incident_id}/actions/start-investigation",
        resources={"incident_id": "synthetic-incident"},
        body=b"{}",
        authorized_at=BASE_TIME,
    )
    with pytest.raises(DBAPIError, match="不能删除已持久化的操作者授权记录"):
        await asyncio.to_thread(command.downgrade, config(), "0060_versioned_runbook_read")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0076_candidate_preflight"
        )
        assert (
            await session.scalar(
                text("SELECT subject_id FROM operator_authorizations WHERE id = :id"),
                {"id": authorization_id},
            )
            == "trusted-engineer"
        )
