"""真实 PostgreSQL 验证动态协调表约束、兼容升级和降级保护。"""

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.coordination.persistence import CoordinationPlanStore
from tests.integration.test_coordination_plan_store import create_args
from tests.task_facts_helpers import TaskClock, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def test_upgrade_keeps_existing_runs_without_backfill(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    await asyncio.to_thread(command.downgrade, config(), "0040_summary_context")
    try:
        await asyncio.to_thread(command.upgrade, config(), "head")
        async with database_session_factory() as session:
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM investigation_coordination_policies")
                )
                == 0
            )
            assert (
                await session.scalar(text("SELECT count(*) FROM investigation_coordination_plans"))
                == 0
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM investigation_runs WHERE id=:id"),
                    {"id": claim.run.id},
                )
                == 1
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


async def test_database_rejects_missing_nested_plan_versions(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    record, _ = await CoordinationPlanStore(
        database_session_factory, clock=TaskClock()
    ).create_plan(claim.run.id, **create_args(claim))
    async with database_session_factory() as session:
        values = (
            (
                await session.execute(
                    text("SELECT * FROM investigation_coordination_plans WHERE round_id=:id"),
                    {"id": record.round_id},
                )
            )
            .mappings()
            .one()
        )
    with pytest.raises(DBAPIError):
        async with database_session_factory() as session, session.begin():
            await session.execute(text("TRUNCATE investigation_coordination_plans"))
            await session.execute(
                text(
                    "INSERT INTO investigation_coordination_plans "
                    "(round_id,investigation_run_id,investigation_attempt_id,version,"
                    "policy_sha256,plan,content_sha256,created_at) VALUES "
                    "(:round_id,:investigation_run_id,:investigation_attempt_id,:version,"
                    ":policy_sha256,CAST(:plan AS jsonb),:content_sha256,:created_at)"
                ),
                {**values, "plan": "{}"},
            )


async def test_downgrade_refuses_saved_coordination_facts(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    await CoordinationPlanStore(database_session_factory, clock=TaskClock()).create_plan(
        claim.run.id, **create_args(claim)
    )
    with pytest.raises(DBAPIError, match="不能删除"):
        await asyncio.to_thread(command.downgrade, config(), "0040_summary_context")
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0061_trusted_operator_identity"
        )
