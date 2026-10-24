"""调查手册策略迁移保留旧事实，并阻止丢失新审计语义。"""

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.loop_helpers import loop_policy
from tests.task_facts_helpers import started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def test_0060_round_trip_preserves_historical_run(database_session_factory):
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="evidence-read-v1",
    )
    async with database_session_factory() as session:
        before = await session.scalar(
            text("SELECT to_jsonb(r) FROM investigation_runs r WHERE id = :id"),
            {"id": started.run.id},
        )

    # 0066 新增列在降级时被移除，单独核对默认值后比较全部旧列。
    assert before.pop("coordination_experience_policy_version") is None
    await asyncio.to_thread(command.downgrade, config(), "0059_telemetry_mcp_metric_policy")
    try:
        async with database_session_factory() as session:
            after = await session.scalar(
                text("SELECT to_jsonb(r) FROM investigation_runs r WHERE id = :id"),
                {"id": started.run.id},
            )
            version = await session.scalar(text("SELECT version_num FROM alembic_version"))
        assert after == before
        assert version == "0059_telemetry_mcp_metric_policy"
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


async def test_0060_refuses_downgrade_after_runbook_policy_is_persisted(
    database_session_factory,
):
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="runbook-read-v1",
    )
    assert started.run.working_summary_policy_version is None
    assert started.run.prompt_version == "runbook-read-v1"

    with pytest.raises(DBAPIError, match="不能删除"):
        await asyncio.to_thread(command.downgrade, config(), "0059_telemetry_mcp_metric_policy")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0073_candidate_plan_admission"
        )
