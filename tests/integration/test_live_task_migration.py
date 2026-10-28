"""固定现场窗口不能通过降级或空值约束漏洞丢失。"""

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from opsswarm.investigation.models import InvestigationDataSourceMode
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.task_evidence_helpers import evidence_attempt, save_evidence
from tests.task_facts_helpers import mark_run_as_legacy_live, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_downgrade_preserves_live_cutoff(database_session_factory):
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        data_source_mode=InvestigationDataSourceMode.LIVE,
    )
    await mark_run_as_legacy_live(database_session_factory, started.run.id)
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    with pytest.raises(DBAPIError, match="不能删除已固定的现场取证窗口"):
        await asyncio.to_thread(command.downgrade, config, "0034_task_evidence_snapshot")
    assert (
        await store.get_run(started.run.id)
    ).observation_cutoff_at == started.run.observation_cutoff_at
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0087_reflow_coordination"
        )


@pytest.mark.parametrize(
    "invalid", ["cutoff_null", "future", "subsecond", "policy_null", "frozen_policy", "budget_null"]
)
async def test_database_rejects_invalid_live_scope(database_session_factory, invalid):
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        data_source_mode=InvestigationDataSourceMode.LIVE,
    )
    values = {
        "cutoff_null": {"observation_cutoff_at": None},
        "future": {"observation_cutoff_at": started.run.created_at + timedelta(seconds=1)},
        "subsecond": {"observation_cutoff_at": started.run.created_at - timedelta(microseconds=1)},
        "policy_null": {"evidence_policy_version": None},
        "frozen_policy": {"evidence_policy_version": "task-evidence-v1"},
        "budget_null": {"call_budget": None},
    }[invalid]
    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == started.run.id)
                .values(**values)
            )
    assert await store.get_run(started.run.id) == started.run


@pytest.mark.parametrize(
    "mode", [InvestigationDataSourceMode.FROZEN, InvestigationDataSourceMode.LIVE]
)
async def test_legacy_v1_has_no_live_scope(database_session_factory, mode):
    store, started = await started_run(database_session_factory, data_source_mode=mode)
    assert started.run.observation_cutoff_at is None
    assert started.run.evidence_policy_version is None
    assert (await store.get_run(started.run.id)).workflow_version is WorkflowVersion.V1


async def test_upgrade_preserves_old_frozen_snapshot_and_live_v1(database_session_factory):
    prepared = await evidence_attempt(database_session_factory)
    saved = await save_evidence(prepared)
    store, old = await started_run(
        database_session_factory, data_source_mode=InvestigationDataSourceMode.LIVE
    )
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    await asyncio.to_thread(command.downgrade, config, "0034_task_evidence_snapshot")
    try:
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "0034_task_evidence_snapshot"
            )
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
    assert (await store.get_run(old.run.id)).observation_cutoff_at is None
    assert (await store.get_run(prepared[1].run.id)).evidence_policy_version == "task-evidence-v1"
    assert (
        await CollaborationQueries(database_session_factory).get_evidence(
            saved.investigation_run_id, saved.task_id
        )
        == saved
    )
