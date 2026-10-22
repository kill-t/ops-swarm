"""真实 PostgreSQL 验证新路由表与元数据一致、空表可独立降级。"""

import asyncio
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_human_routing_metadata_and_empty_roundtrip(database_session_factory):
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))
    await asyncio.to_thread(command.check, config)
    try:
        await asyncio.to_thread(command.downgrade, config, "0070_human_input_reassessment")
        async with database_session_factory() as session:
            assert (
                await session.scalar(
                    text("SELECT to_regclass('investigation_human_review_routes')")
                )
                is None
            )
            assert (
                await session.scalar(text("SELECT version_num FROM alembic_version"))
                == "0070_human_input_reassessment"
            )
    finally:
        await asyncio.to_thread(command.upgrade, config, "head")
    await asyncio.to_thread(command.check, config)
