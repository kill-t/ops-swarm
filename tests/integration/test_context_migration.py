"""上下文迁移不回填历史策略，且拒绝删除已保存的审计依据。"""

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.integration.test_context_reservations import prepared_context, reserve_context
from tests.model_budget_helpers import model_policy
from tests.task_facts_helpers import started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_upgrade_keeps_old_run_context_unknown(database_session_factory):
    _, started = await started_run(database_session_factory)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    await asyncio.to_thread(command.downgrade, config, "0035_live_task_evidence")
    await asyncio.to_thread(command.upgrade, config, "head")
    async with database_session_factory() as session:
        run = await session.get(InvestigationRunRow, started.run.id)
        assert run.context_policy_version is None


@pytest.mark.parametrize("audit", ["policy", "manifest"])
async def test_downgrade_cannot_erase_context_audit(database_session_factory, audit):
    if audit == "policy":
        await started_run(
            database_session_factory,
            workflow_version=WorkflowVersion.V2,
            model_budget=model_policy(),
        )
    else:
        prepared, packet, store = await prepared_context(database_session_factory)
        await reserve_context(store, prepared, packet)
        # 即使策略字段被旧运维脚本清空，已提交清单仍阻止降级。
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(
                    InvestigationRunRow.id == prepared[1].run.id,
                )
                .values(context_policy_version=None)
            )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with pytest.raises(DBAPIError, match="不能删除"):
        await asyncio.to_thread(command.downgrade, config, "0035_live_task_evidence")
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0054_third_round_investigation"
        )
