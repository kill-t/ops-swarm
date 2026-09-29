"""真实 PostgreSQL 验证旧策略保留、摘要归属约束和审计降级保护。"""

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text, update
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.loop_helpers import loop_policy
from tests.task_facts_helpers import started_run
from tests.working_summary_helpers import analyzed_task, finish

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def config():
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def test_upgrade_keeps_old_loop_policy_and_never_backfills(database_session_factory):
    db = database_session_factory
    _, started = await started_run(
        db,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="evidence-read-v1",
    )
    async with db() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow).values(working_summary_policy_version=None)
        )
        before = await session.scalar(
            text(
                "SELECT to_jsonb(r) - 'working_summary_policy_version' "
                "FROM investigation_runs r WHERE id=:id"
            ),
            {"id": started.run.id},
        )
    await asyncio.to_thread(command.downgrade, config(), "0038_evidence_read_loop")
    try:
        await asyncio.to_thread(command.upgrade, config(), "head")
        async with db() as session:
            after = await session.scalar(
                text(
                    "SELECT to_jsonb(r) - 'working_summary_policy_version' "
                    "FROM investigation_runs r WHERE id=:id"
                ),
                {"id": started.run.id},
            )
            assert after == before
            assert (
                await session.scalar(
                    text("SELECT working_summary_policy_version FROM investigation_runs")
                )
                is None
            )
            assert (
                await session.scalar(text("SELECT count(*) FROM agent_task_working_summaries")) == 0
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


@pytest.mark.parametrize("with_summary", [False, True])
async def test_downgrade_refuses_policy_or_saved_material(database_session_factory, with_summary):
    db = database_session_factory
    if with_summary:
        prepared, result = await analyzed_task(db)
        await finish(prepared, result)
        # 即使运行策略被数据库维护改空，也不能丢掉已有材料。
        async with db() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow).values(working_summary_policy_version=None)
            )
    else:
        await started_run(
            db,
            workflow_version=WorkflowVersion.V2,
            model_budget=loop_policy(),
            context_policy_version="evidence-read-v1",
        )
    with pytest.raises(DBAPIError, match="不能删除"):
        await asyncio.to_thread(command.downgrade, config(), "0038_evidence_read_loop")
    async with db() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0051_second_round_review"
        )


@pytest.mark.parametrize("context", [None, "task-context-v1"])
async def test_summary_policy_requires_supported_context(database_session_factory, context):
    db = database_session_factory
    await started_run(db, workflow_version=WorkflowVersion.V2, model_budget=loop_policy())
    with pytest.raises(DBAPIError):
        async with db() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow).values(
                    working_summary_policy_version="task-working-summary-v1",
                    context_policy_version=context,
                )
            )


@pytest.mark.parametrize(
    "column,value",
    [
        ("version", "future"),
        ("content_sha256", "bad"),
        ("summary", "[]"),
        ("summary", '{"large":"' + "x" * 262144 + '"}'),
    ],
    ids=["version", "hash", "json_type", "size"],
)
async def test_database_rejects_invalid_summary_insert(database_session_factory, column, value):
    db = database_session_factory
    prepared, result = await analyzed_task(db)
    await finish(prepared, result)
    async with db() as session, session.begin():
        # 仅清空隔离测试的摘要以测试 INSERT 约束，不绕过不可变 UPDATE 触发器。
        saved = (
            (await session.execute(text("SELECT * FROM agent_task_working_summaries")))
            .mappings()
            .one()
        )
        values = dict(saved)
        await session.execute(text("TRUNCATE agent_task_working_summaries"))
    import json

    values["summary"] = json.dumps(values["summary"])
    values[column] = value
    with pytest.raises(DBAPIError):
        async with db() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO agent_task_working_summaries "
                    "(task_id, investigation_run_id, attempt_id, version, summary, "
                    "content_sha256, created_at) "
                    "VALUES (:task_id,:investigation_run_id,:attempt_id,:version,"
                    "CAST(:summary AS jsonb),:content_sha256,:created_at)"
                ),
                values,
            )
