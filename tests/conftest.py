import asyncio
import os
import sys
from collections.abc import AsyncIterator, Callable, Iterator, Mapping
from pathlib import Path

import psycopg
import pytest
import pytest_asyncio
from alembic.config import Config
from psycopg import sql
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from testcontainers.community.postgres import PostgresContainer

from alembic import command
from opsswarm.database.asyncio_compat import create_selector_event_loop

PROJECT_ROOT = Path(__file__).resolve().parents[1]
POSTGRES_IMAGE = "postgres:17.6-alpine"


def pytest_asyncio_loop_factories(
    config: pytest.Config,
    item: pytest.Item,
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """让 Windows 集成测试使用 Psycopg 支持的 Selector 事件循环。"""

    del config, item
    if sys.platform == "win32":
        return {"windows-selector": create_selector_event_loop}
    return {"default": asyncio.new_event_loop}


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


@pytest.fixture(scope="session")
def checkpoint_database_url(migrated_database_url: str) -> Iterator[str]:
    """在同一临时 PostgreSQL 实例中创建隔离的 checkpoint 数据库。"""

    source_url = make_url(migrated_database_url)
    checkpoint_name = "opsswarm_checkpoints"
    admin_url = source_url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    checkpoint_url = source_url.set(
        drivername="postgresql",
        database=checkpoint_name,
    ).render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(checkpoint_name)))
    yield checkpoint_url


@pytest_asyncio.fixture
async def database_session_factory(
    migrated_database_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """每个测试清空隔离数据库，并提供绑定当前事件循环的会话工厂。"""

    engine = create_async_engine(migrated_database_url, pool_pre_ping=True)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "TRUNCATE TABLE change_records, evaluation_attempts, evaluation_results, "
                "evaluation_runs, "
                "evaluation_experiments, "
                "evaluation_cases, "
                "work_items, investigation_runs, incident_events, alerts, incidents "
                "RESTART IDENTITY CASCADE"
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()
