from pathlib import Path

import pytest
from alembic.config import Config

from alembic import command

pytestmark = pytest.mark.integration

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_alembic_migration_matches_sqlalchemy_metadata(migrated_database_url: str) -> None:
    """防止手写迁移与 ORM 映射在后续修改中静默漂移。"""

    assert migrated_database_url.startswith("postgresql+psycopg://")
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    command.check(alembic_config)


def test_alembic_migration_can_downgrade_and_upgrade(migrated_database_url: str) -> None:
    """验证开发和故障恢复场景下迁移具备真实回退路径。"""

    assert migrated_database_url.startswith("postgresql+psycopg://")
    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))

    command.downgrade(alembic_config, "base")
    command.upgrade(alembic_config, "head")
    command.check(alembic_config)
