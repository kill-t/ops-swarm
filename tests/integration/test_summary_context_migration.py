"""真实 PostgreSQL 验证摘要上下文策略组合、兼容升级和降级保护。"""

import asyncio
import json
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.integration.test_call_budget_store import prepare
from tests.integration.test_repeated_calls import reserve_slot
from tests.loop_helpers import loop_policy
from tests.model_budget_helpers import model_input, model_policy
from tests.task_facts_helpers import started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def test_upgrade_keeps_old_loop_run_without_backfill(database_session_factory):
    db = database_session_factory
    _, started = await started_run(
        db,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="evidence-read-v1",
    )
    async with db() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(working_summary_policy_version=None)
        )
        before = await session.scalar(
            text("SELECT to_jsonb(r) FROM investigation_runs r WHERE id=:id"),
            {"id": started.run.id},
        )

    await asyncio.to_thread(command.downgrade, config(), "0039_task_working_summary")
    try:
        await asyncio.to_thread(command.upgrade, config(), "head")
        async with db() as session:
            after = await session.scalar(
                text("SELECT to_jsonb(r) FROM investigation_runs r WHERE id=:id"),
                {"id": started.run.id},
            )
        assert after == before
        assert after["context_policy_version"] == "evidence-read-v1"
        assert after["working_summary_policy_version"] is None
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


async def test_summary_context_requires_summary_policy(database_session_factory):
    db = database_session_factory
    _, started = await started_run(
        db,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="evidence-read-v1",
    )
    async with db() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(working_summary_policy_version=None)
        )

    with pytest.raises(IntegrityError):
        async with db() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == started.run.id)
                .values(context_policy_version="summary-context-v1")
            )

    async with db() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == started.run.id)
            .values(
                context_policy_version="summary-context-v1",
                working_summary_policy_version="task-working-summary-v1",
            )
        )


async def test_summary_context_requires_function_calling_profile(database_session_factory):
    db = database_session_factory
    _, started = await started_run(
        db,
        workflow_version=WorkflowVersion.V2,
        model_budget=model_policy(),
        context_policy_version="task-context-v1",
    )
    with pytest.raises(IntegrityError):
        async with db() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == started.run.id)
                .values(
                    context_policy_version="summary-context-v1",
                    working_summary_policy_version="task-working-summary-v1",
                )
            )


@pytest.mark.parametrize(
    "audit",
    [
        "run_policy",
        "run_prompt",
        "manifest_version",
        "manifest_prompt",
        "allocation_prompt",
    ],
)
async def test_downgrade_refuses_summary_context_audit(database_session_factory, audit):
    db = database_session_factory
    if audit == "run_policy":
        _, started = await started_run(
            db,
            workflow_version=WorkflowVersion.V2,
            model_budget=loop_policy(),
            context_policy_version="evidence-read-v1",
        )
        async with db() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == started.run.id)
                .values(context_policy_version="summary-context-v1")
            )
    elif audit == "run_prompt":
        _, started = await started_run(db, workflow_version=WorkflowVersion.V2)
        async with db() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == started.run.id)
                .values(prompt_version="summary-context-v1")
            )
    else:
        _, claim, _, _, attempts, store = await prepare(db, model_budget=model_policy())
        grant = await reserve_slot(
            store,
            claim,
            attempts["log"],
            0,
            operation=CallOperation.ANALYZE_LOG,
            model_input=model_input(),
            prompt_version="fixed-multi-agent-v1",
        )
        manifest = {
            "version": "summary-context-v1" if audit == "manifest_version" else "task-context-v1",
            "prompt_version": (
                "summary-context-v1" if audit == "manifest_prompt" else "fixed-task-context-v1"
            ),
        }
        async with db() as session, session.begin():
            if audit == "allocation_prompt":
                await session.execute(
                    text(
                        "UPDATE investigation_call_reservations "
                        "SET model_allocation=jsonb_set(model_allocation, '{prompt_version}', "
                        "'\"summary-context-v1\"'::jsonb) WHERE id=:call_id"
                    ),
                    {"call_id": grant.record.id},
                )
            else:
                await session.execute(
                    text(
                        "UPDATE investigation_call_reservations "
                        "SET context_manifest=CAST(:manifest AS JSONB) WHERE id=:call_id"
                    ),
                    {"manifest": json.dumps(manifest), "call_id": grant.record.id},
                )

    with pytest.raises(DBAPIError, match="不能删除"):
        await asyncio.to_thread(command.downgrade, config(), "0039_task_working_summary")
    async with db() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0082_candidate_release_job"
        )
