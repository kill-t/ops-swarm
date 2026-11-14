"""独立空容器中构造 0029 业务事实，再验证向前升级不改写原记录。"""

from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

from alembic import command

pytestmark = pytest.mark.integration
ROOT = Path(__file__).resolve().parents[2]


def test_0029_incident_alert_and_timeline_survive_upgrade(monkeypatch):
    """不用现有业务卷；身份、时间、正文及审计序号必须原样保留。"""
    with PostgresContainer("postgres:17.6-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url()
        monkeypatch.setenv("OPSSWARM_DATABASE_URL", url)
        config = Config(str(ROOT / "alembic.ini"))
        command.upgrade(config, "0029_follow_up_governance_idx")
        native = make_url(url).set(drivername="postgresql").render_as_string(hide_password=False)
        incident_id, alert_id = uuid4(), uuid4()
        with psycopg.connect(native) as connection:
            connection.execute(
                "INSERT INTO incidents (id, service_name, title, severity, status, fingerprint, "
                "version, started_at, created_at, updated_at) "
                "VALUES (%s, 'order-service', '旧事故', 'p1', 'open', 'upgrade-case', "
                "1, now(), now(), now())",
                (incident_id,),
            )
            connection.execute(
                "INSERT INTO alerts (id, incident_id, source, external_event_id, fingerprint, "
                "service_name, severity, title, payload, occurred_at, ingested_at) "
                "VALUES (%s, %s, 'upgrade-test', 'old-event', 'upgrade-case', 'order-service', "
                "'p1', '旧告警', '{\"original\":true}', now(), now())",
                (alert_id, incident_id),
            )
            connection.execute(
                "INSERT INTO incident_events (incident_id, event_type, actor_type, to_status, "
                "reason, incident_version, payload, occurred_at) "
                "VALUES (%s, 'incident.created', 'system', 'open', '旧审计', 1, '{}', now())",
                (incident_id,),
            )
        queries = (
            "SELECT id, display_number, title, status, version, started_at, created_at, updated_at "
            "FROM incidents ORDER BY display_number",
            "SELECT id, incident_id, external_event_id, payload, occurred_at, ingested_at "
            "FROM alerts",
            "SELECT id, incident_id, event_type, reason, incident_version, payload, occurred_at "
            "FROM incident_events ORDER BY id",
        )
        with psycopg.connect(native) as connection:
            before = [connection.execute(query).fetchall() for query in queries]
        command.upgrade(config, "head")
        command.check(config)
        with psycopg.connect(native) as connection:
            assert [connection.execute(query).fetchall() for query in queries] == before
            assert connection.execute("SELECT version_num FROM alembic_version").fetchone() == (
                ScriptDirectory.from_config(config).get_current_head(),
            )
