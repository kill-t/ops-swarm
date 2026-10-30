"""遥测 MCP 策略在真实 PostgreSQL 中保持升级与降级边界。"""

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.models import InvestigationDataSourceMode
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.task_facts_helpers import (
    mark_run_as_legacy_live,
    mark_run_as_mcp_log_live,
    started_run,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def migration_config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def test_new_live_run_pins_mcp_policy_and_blocks_downgrade(
    database_session_factory,
) -> None:
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        data_source_mode=InvestigationDataSourceMode.LIVE,
    )

    assert started.run.evidence_policy_version == "task-evidence-live-mcp-v1"
    with pytest.raises(DBAPIError, match="telemetry MCP metric policy"):
        await asyncio.to_thread(
            command.downgrade,
            migration_config(),
            "0058_telemetry_mcp_log_policy",
        )

    assert (await store.get_run(started.run.id)).evidence_policy_version == (
        "task-evidence-live-mcp-v1"
    )
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0093_reflow_candidate"
        )


@pytest.mark.parametrize("historical_policy", ["direct", "mcp_log"])
async def test_historical_live_policies_can_cross_new_migration(
    database_session_factory, historical_policy
) -> None:
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        data_source_mode=InvestigationDataSourceMode.LIVE,
    )
    if historical_policy == "direct":
        await mark_run_as_legacy_live(database_session_factory, started.run.id)
        expected = "task-evidence-live-v1"
    else:
        await mark_run_as_mcp_log_live(database_session_factory, started.run.id)
        expected = "task-evidence-live-mcp-log-v1"

    await asyncio.to_thread(
        command.downgrade,
        migration_config(),
        "0058_telemetry_mcp_log_policy",
    )
    try:
        # 降级库不含后续新增列，只读取该历史迁移定义的字段。
        async with database_session_factory() as session:
            assert (
                await session.scalar(
                    text("SELECT evidence_policy_version FROM investigation_runs WHERE id=:id"),
                    {"id": started.run.id},
                )
                == expected
            )
    finally:
        await asyncio.to_thread(command.upgrade, migration_config(), "head")
    assert (await store.get_run(started.run.id)).evidence_policy_version == expected
