"""独立审查原生迁移与空库可逆性。"""

import asyncio

import pytest
from sqlalchemy import text

from alembic import command
from tests.integration.test_candidate_approval_migration import config

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_0091_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0090_reflow_synthesis")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT to_regclass('investigation_reflow_review_jobs')"))
                is None
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
    await asyncio.to_thread(command.check, config())
