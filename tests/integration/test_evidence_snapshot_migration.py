"""新取证策略固定后，降级不能丢失运行语义。"""

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.task_facts_helpers import started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_downgrade_refuses_to_remove_pinned_evidence_policy(database_session_factory):
    store, started = await started_run(
        database_session_factory, workflow_version=WorkflowVersion.V2
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with pytest.raises(DBAPIError, match="不能删除已固定的取证策略"):
        await asyncio.to_thread(command.downgrade, config, "0033_model_input_budget")
    assert (await store.get_run(started.run.id)).evidence_policy_version == "task-evidence-v1"
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0051_second_round_review"
        )
