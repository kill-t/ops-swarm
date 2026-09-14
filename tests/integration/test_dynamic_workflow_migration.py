"""真实 PostgreSQL 验证 v3 启动契约迁移与降级保护。"""

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.loop_helpers import loop_policy
from tests.task_facts_helpers import started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def test_dynamic_policy_constraint_rejects_partial_v3_snapshot(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="summary-context-v1",
    )

    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == started.run.id)
                .values(workflow_version="v3")
            )


async def test_dynamic_policy_constraint_rejects_graph_version_mutation(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )

    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == started.run.id)
                .values(graph_version="unknown-graph-v1")
            )


async def test_migration_preserves_v2_when_downgraded_and_upgraded(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="summary-context-v1",
    )

    await asyncio.to_thread(command.downgrade, config(), "0042_coordination_model_call")
    try:
        async with database_session_factory() as session:
            row = (
                (
                    await session.execute(
                        text(
                            "SELECT workflow_version, context_policy_version "
                            "FROM investigation_runs WHERE id=:run_id"
                        ),
                        {"run_id": started.run.id},
                    )
                )
                .mappings()
                .one()
            )
        assert row["workflow_version"] == "v2"
        assert row["context_policy_version"] == "summary-context-v1"
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


async def test_fanout_version_downgrade_refuses_persisted_v2_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )

    with pytest.raises(DBAPIError, match="不能删除已保存的动态专业分派图版本"):
        await asyncio.to_thread(command.downgrade, config(), "0043_dynamic_workflow_start")

    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0044_dynamic_graph_fanout"
        )
        assert (
            await session.scalar(
                text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                {"run_id": started.run.id},
            )
            == "dynamic-investigation-graph-v2"
        )


async def test_fanout_version_downgrade_preserves_legacy_v1_graph(
    database_session_factory,
) -> None:
    _, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V3,
        model_budget=loop_policy(),
    )
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(graph_version="dynamic-investigation-graph-v1")
        )

    await asyncio.to_thread(command.downgrade, config(), "0043_dynamic_workflow_start")
    try:
        async with database_session_factory() as session:
            assert (
                await session.scalar(
                    text("SELECT graph_version FROM investigation_runs WHERE id=:run_id"),
                    {"run_id": started.run.id},
                )
                == "dynamic-investigation-graph-v1"
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
