"""真实 PostgreSQL 验证终端交接迁移及跨表提交护栏。"""

import asyncio
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from tests.integration.test_coordination_experience_migration import claim_v3, start_v3_run
from tests.task_facts_helpers import BASE_TIME

pytestmark = pytest.mark.integration


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


@pytest.mark.asyncio
async def test_0067_metadata_and_empty_downgrade_upgrade(database_session_factory) -> None:
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0066_coordination_experience")
        async with database_session_factory() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0066_coordination_experience"
            )
            assert (
                await session.scalar(text("SELECT to_regclass('investigation_terminal_handoffs')"))
                is None
            )
        await asyncio.to_thread(command.upgrade, config(), "head")
        await asyncio.to_thread(command.check, config())
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


@pytest.mark.asyncio
async def test_0067_native_guards_and_active_incident_index(database_session_factory) -> None:
    async with database_session_factory() as session:
        triggers = (
            await session.execute(
                text(
                    "SELECT tgname, tgdeferrable, tginitdeferred FROM pg_trigger "
                    "WHERE tgname IN ('terminal_handoffs_state', "
                    "'investigation_runs_handoff_state', 'work_items_handoff_state', "
                    "'investigation_attempts_handoff_state')"
                )
            )
        ).all()
        assert len(triggers) == 4
        assert all(deferred and initially_deferred for _, deferred, initially_deferred in triggers)
        immediate = set(
            await session.scalars(
                text(
                    "SELECT tgname FROM pg_trigger WHERE tgname IN "
                    "('terminal_handoffs_source', 'terminal_handoffs_immutable') "
                    "AND NOT tgdeferrable"
                )
            )
        )
        assert immediate == {"terminal_handoffs_source", "terminal_handoffs_immutable"}
        index = await session.scalar(
            text(
                "SELECT indexdef FROM pg_indexes "
                "WHERE indexname='uq_investigation_runs_active_incident'"
            )
        )
        assert "await_human" in index and "await_candidate" in index
        constraints = dict(
            (
                await session.execute(
                    text(
                        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname IN ('ck_investigation_runs_handoff_scope', "
                        "'ck_investigation_runs_v3_no_success', 'ck_task_events_type')"
                    )
                )
            ).all()
        )
        assert (
            "dynamic-investigation-graph-v15" in constraints["ck_investigation_runs_handoff_scope"]
        )
        assert "succeeded" in constraints["ck_investigation_runs_v3_no_success"]
        assert "run.handed_off" in constraints["ck_task_events_type"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["await_human", "await_candidate"])
async def test_0067_waiting_run_requires_handoff_at_commit(
    database_session_factory, status
) -> None:
    store, started = await start_v3_run(database_session_factory, experience=False)
    await claim_v3(store)
    with pytest.raises(DBAPIError, match="缺少终端交接事实"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text("UPDATE investigation_runs SET status=:status WHERE id=:id"),
                {"id": started.run.id, "status": status},
            )
    async with database_session_factory() as session:
        assert (
            await session.scalar(
                text("SELECT status FROM investigation_runs WHERE id=:id"),
                {"id": started.run.id},
            )
            == "running"
        )


@pytest.mark.asyncio
async def test_0067_handed_off_attempt_requires_handoff_at_commit(database_session_factory) -> None:
    store, _ = await start_v3_run(database_session_factory, experience=False)
    claim = await claim_v3(store)
    with pytest.raises(DBAPIError, match="缺少终端交接事实"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_attempts SET status='handed_off', "
                    "finished_at=:now, duration_ms=1000, will_retry=false WHERE id=:id"
                ),
                {"id": claim.attempt.id, "now": BASE_TIME + timedelta(seconds=3)},
            )


@pytest.mark.asyncio
async def test_0067_v3_cannot_bypass_handoff_as_old_success(database_session_factory) -> None:
    store, started = await start_v3_run(database_session_factory, experience=False)
    await claim_v3(store)
    with pytest.raises(DBAPIError, match="ck_investigation_runs_v3_no_success"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "UPDATE investigation_runs SET status='succeeded', result='{}'::jsonb, "
                    "metrics='{}'::jsonb, completed_at=:now WHERE id=:id"
                ),
                {"id": started.run.id, "now": BASE_TIME + timedelta(seconds=3)},
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", ["lease_expired", "foreign_attempt", "missing_route"])
async def test_0067_insert_requires_current_lease_and_persisted_route(
    database_session_factory, invalid
) -> None:
    store, started = await start_v3_run(database_session_factory, experience=False)
    claim = await claim_v3(store)
    now = BASE_TIME + timedelta(seconds=3 if invalid != "lease_expired" else 33)
    expected = "来源路由不匹配" if invalid == "missing_route" else "当前有效父租约"
    with pytest.raises(DBAPIError, match=expected):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO investigation_terminal_handoffs "
                    "(id, investigation_run_id, investigation_attempt_id, source_review_route_id, "
                    "source_sha256, version, destination, created_at) "
                    "VALUES (:id,:run,:attempt,:route,:hash,"
                    "'terminal-handoff-v1','await_human',:now)"
                ),
                {
                    "id": uuid4(),
                    "run": started.run.id,
                    "attempt": uuid4() if invalid == "foreign_attempt" else claim.attempt.id,
                    "route": uuid4(),
                    "hash": "a" * 64,
                    "now": now,
                },
            )


@pytest.mark.asyncio
async def test_0067_downgrade_preserves_handoff_events(database_session_factory) -> None:
    _, started = await start_v3_run(database_session_factory, experience=False)
    async with database_session_factory() as session, session.begin():
        await session.execute(
            text(
                "INSERT INTO agent_task_events "
                "(investigation_run_id,cursor,event_type,occurred_at) "
                "VALUES (:run,1,'run.handed_off',:now)"
            ),
            {"run": started.run.id, "now": BASE_TIME + timedelta(seconds=3)},
        )
    with pytest.raises(Exception, match="不能删除已保存的终端交接事实"):
        await asyncio.to_thread(command.downgrade, config(), "0066_coordination_experience")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0071_human_reassessment_routing"
        )
        assert await session.scalar(text("SELECT count(*) FROM agent_task_events")) == 1
