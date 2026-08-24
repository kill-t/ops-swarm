import os
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest
import pytest_asyncio
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from alembic import command

PROJECT_ROOT = Path(__file__).resolve().parents[2]
POSTGRES_IMAGE = "postgres:17.6-alpine"


@pytest.fixture(scope="session")
def migrated_database_url() -> Iterator[str]:
    """为整次测试启动一个隔离 PostgreSQL，并执行正式 Alembic 迁移。"""

    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url()
        previous_url = os.environ.get("OPSSWARM_DATABASE_URL")
        os.environ["OPSSWARM_DATABASE_URL"] = database_url
        try:
            alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
            command.upgrade(alembic_config, "head")
            yield database_url
        finally:
            if previous_url is None:
                os.environ.pop("OPSSWARM_DATABASE_URL", None)
            else:
                os.environ["OPSSWARM_DATABASE_URL"] = previous_url


@pytest_asyncio.fixture
async def database_session_factory(
    migrated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """每个测试清空隔离数据库，并提供绑定当前事件循环的会话工厂。"""

    engine = create_async_engine(migrated_database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text("TRUNCATE TABLE incident_events, alerts, incidents RESTART IDENTITY CASCADE")
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()
