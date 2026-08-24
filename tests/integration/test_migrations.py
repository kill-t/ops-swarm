from pathlib import Path

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy.engine import make_url

from alembic import command

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def truncate_business_data(database_url: str) -> None:
    """schema 回退测试只验证空库迁移，不删除真实或共享环境数据。"""

    native_url = (
        make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)
    )
    with psycopg.connect(native_url, autocommit=True) as connection:
        connection.execute(
            "TRUNCATE TABLE work_items, investigation_runs, incident_events, alerts, "
            "incidents RESTART IDENTITY CASCADE"
        )


def test_alembic_migration_matches_sqlalchemy_metadata(migrated_database_url: str) -> None:
    """防止手写迁移与 ORM 映射在后续修改中静默漂移。"""

    assert migrated_database_url.startswith("postgresql+psycopg://")
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.check(alembic_config)


def test_alembic_migration_can_downgrade_and_upgrade(migrated_database_url: str) -> None:
    """验证开发和故障恢复场景下迁移具备真实回退路径。"""

    assert migrated_database_url.startswith("postgresql+psycopg://")
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))

    truncate_business_data(migrated_database_url)
    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    command.check(alembic_config)
