from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from psycopg.types.json import Jsonb
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
            "TRUNCATE TABLE change_records, investigation_attempts, "
            "evaluation_attempts, evaluation_results, "
            "evaluation_runs, "
            "evaluation_experiments, evaluation_cases, work_items, investigation_runs, "
            "incident_events, alerts, "
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


def test_artifact_contract_migration_refuses_to_drop_v2_audit_fact(
    migrated_database_url: str,
) -> None:
    """已有新契约事实时不能降级并丢失其契约版本。"""

    native_url = (
        make_url(migrated_database_url)
        .set(drivername="postgresql")
        .render_as_string(hide_password=False)
    )
    record_id = uuid4()
    details = {
        "change_type": "deployment",
        "external_change_id": "migration-v2-audit",
        "from_version": "stable",
        "to_version": "rc1",
        "code_reference": {
            "repository": "kill-t/ops-swarm",
            "commit_sha": "a" * 40,
        },
        "artifact_reference": {
            "image_repository": "ghcr.io/kill-t/ops-swarm-order-service",
            "image_digest": "sha256:" + "b" * 64,
        },
        "completed": True,
    }
    with psycopg.connect(native_url, autocommit=True) as connection:
        connection.execute(
            """
            INSERT INTO change_records (
                id, source, source_identity, source_contract_version,
                external_change_id, service_name, change_type, details,
                content_hash, occurred_at, recorded_at
            ) VALUES (
                %s, 'ci-deployment-ingestion', 'migration-test', 2,
                'migration-v2-audit', 'order-service', 'deployment', %s,
                %s, now(), now()
            )
            """,
            (record_id, Jsonb(details), "0" * 64),
        )

    alembic_config = Config(str(PROJECT_ROOT / "alembic.ini"))
    try:
        with pytest.raises(RuntimeError, match="不能降级"):
            command.downgrade(alembic_config, "0025_ci_deployment_ingestion")
    finally:
        with psycopg.connect(native_url, autocommit=True) as connection:
            connection.execute("DELETE FROM change_records WHERE id = %s", (record_id,))

    command.check(alembic_config)
