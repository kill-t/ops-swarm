"""已启用模型预算的运行不能通过降级丢失审计事实。"""

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.model_budget_helpers import model_policy
from tests.task_facts_helpers import started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_downgrade_refuses_to_remove_pinned_model_budget(database_session_factory):
    store, started = await started_run(
        database_session_factory, workflow_version=WorkflowVersion.V2, model_budget=model_policy()
    )
    # 让本测试实际触达模型预算的降级护栏，取证策略另有独立测试。
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(evidence_policy_version=None, context_policy_version=None)
        )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with pytest.raises(DBAPIError, match="不能删除"):
        await asyncio.to_thread(command.downgrade, config, "0032_shared_call_budget")
    assert (await store.get_run(started.run.id)).model_budget == model_policy()
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0042_coordination_model_call"
        )
