"""已启用模型预算的运行不能通过降级丢失审计事实。"""

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.model_budget_helpers import model_policy
from tests.task_facts_helpers import started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_downgrade_refuses_to_remove_pinned_model_budget(database_session_factory):
    store, started = await started_run(
        database_session_factory, workflow_version=WorkflowVersion.V2, model_budget=model_policy()
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with pytest.raises(DBAPIError, match="不能删除"):
        await asyncio.to_thread(command.downgrade, config, "0032_shared_call_budget")
    assert (await store.get_run(started.run.id)).model_budget == model_policy()
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0033_model_input_budget"
        )
