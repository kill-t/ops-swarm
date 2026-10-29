"""调用身份迁移保留旧事实，拒绝降级丢失槽位。"""

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from alembic import command
from tests.integration.test_call_budget_store import prepare
from tests.integration.test_repeated_calls import reserve_slot

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def config():
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def test_upgrade_preserves_original_key_and_call(database_session_factory):
    _, claim, _, _, attempts, store = await prepare(database_session_factory)
    grant = await reserve_slot(store, claim, attempts["log"], 0)
    async with database_session_factory() as session:
        before = (
            await session.execute(
                text(
                    "SELECT to_jsonb(c) - 'invocation_index' FROM investigation_call_reservations c"
                )
            )
        ).scalar_one()
    await asyncio.to_thread(command.downgrade, config(), "0036_task_context_manifest")
    await asyncio.to_thread(command.upgrade, config(), "head")
    replay = await reserve_slot(store, claim, attempts["log"], 0)
    assert not replay.created and replay.record.id == grant.record.id
    assert replay.record.invocation_index == 0
    async with database_session_factory() as session:
        after = (
            await session.execute(
                text(
                    "SELECT to_jsonb(c) - 'invocation_index' FROM investigation_call_reservations c"
                )
            )
        ).scalar_one()
    assert before == after


async def test_downgrade_refuses_nonzero_slot(database_session_factory):
    _, claim, _, _, attempts, store = await prepare(database_session_factory)
    await reserve_slot(store, claim, attempts["log"], 1)
    with pytest.raises(DBAPIError, match="不能删除"):
        await asyncio.to_thread(command.downgrade, config(), "0036_task_context_manifest")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0092_reflow_routing"
        )
    assert not (await reserve_slot(store, claim, attempts["log"], 1)).created


@pytest.mark.parametrize(
    "change",
    [
        "invocation_index = -1",
        "invocation_index = 100",
        "invocation_index = NULL",
        "invocation_index = 1",
        "idempotency_key = 'forged'",
    ],
)
async def test_database_rejects_invalid_or_inconsistent_identity(database_session_factory, change):
    _, claim, _, _, attempts, store = await prepare(database_session_factory)
    await reserve_slot(store, claim, attempts["log"], 0)
    with pytest.raises(IntegrityError):
        async with database_session_factory() as session, session.begin():
            # 仅使用测试内固定 SQL 片段，模拟绕过仓储的不合法写入。
            await session.execute(text(f"UPDATE investigation_call_reservations SET {change}"))
