"""创建 Incident、Alert 与时间线事件表。

版本标识：0001_incident_storage
前置版本：无
创建时间：2026-08-24
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0001_incident_storage"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

INCIDENT_STATUS_SQL = (
    "'open', 'investigating', 'awaiting_approval', 'mitigating', 'verifying', 'resolved', 'closed'"
)


def upgrade() -> None:
    """建立第一批业务事实与审计表。"""

    op.create_table(
        "incidents",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "display_number",
            sa.BigInteger(),
            sa.Identity(always=True),
            nullable=False,
        ),
        sa.Column("service_name", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("severity", sa.String(length=2), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("fingerprint", sa.String(length=200), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("severity IN ('p1', 'p2', 'p3', 'p4')", name="ck_incidents_severity"),
        sa.CheckConstraint(f"status IN ({INCIDENT_STATUS_SQL})", name="ck_incidents_status"),
        sa.CheckConstraint("version >= 1", name="ck_incidents_version_positive"),
        sa.CheckConstraint("created_at <= updated_at", name="ck_incidents_timestamp_order"),
        sa.CheckConstraint(
            "((status IN ('resolved', 'closed')) AND resolved_at IS NOT NULL) OR "
            "((status NOT IN ('resolved', 'closed')) AND resolved_at IS NULL)",
            name="ck_incidents_resolved_time",
        ),
        sa.CheckConstraint(
            "(status = 'closed' AND closed_at IS NOT NULL) OR "
            "(status <> 'closed' AND closed_at IS NULL)",
            name="ck_incidents_closed_time",
        ),
        sa.CheckConstraint(
            "resolved_at IS NULL OR resolved_at <= updated_at",
            name="ck_incidents_resolved_before_update",
        ),
        sa.CheckConstraint(
            "closed_at IS NULL OR closed_at <= updated_at",
            name="ck_incidents_closed_before_update",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("display_number", name="uq_incidents_display_number"),
    )
    op.create_index(
        "uq_incidents_active_fingerprint",
        "incidents",
        ["fingerprint"],
        unique=True,
        postgresql_where=sa.text("status <> 'closed'"),
    )
    op.create_index(
        "ix_incidents_status_created_at",
        "incidents",
        ["status", sa.literal_column("created_at DESC")],
        unique=False,
    )

    op.create_table(
        "alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("source", sa.String(length=100), nullable=False),
        sa.Column("external_event_id", sa.String(length=200), nullable=False),
        sa.Column("fingerprint", sa.String(length=200), nullable=False),
        sa.Column("service_name", sa.String(length=100), nullable=False),
        sa.Column("severity", sa.String(length=2), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ingested_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("severity IN ('p1', 'p2', 'p3', 'p4')", name="ck_alerts_severity"),
        sa.CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_alerts_payload_object"),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source",
            "external_event_id",
            name="uq_alerts_source_external_event_id",
        ),
    )
    op.create_index(
        "ix_alerts_incident_occurred_at",
        "alerts",
        ["incident_id", "occurred_at"],
        unique=False,
    )

    op.create_table(
        "incident_events",
        sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("actor_type", sa.String(length=20), nullable=False),
        sa.Column("actor_id", sa.String(length=100), nullable=True),
        sa.Column("from_status", sa.String(length=32), nullable=True),
        sa.Column("to_status", sa.String(length=32), nullable=False),
        sa.Column("reason", sa.String(length=500), nullable=False),
        sa.Column("incident_version", sa.Integer(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "event_type IN ('incident.created', 'incident.status_changed', "
            "'incident.alert_attached')",
            name="ck_incident_events_type",
        ),
        sa.CheckConstraint(
            "actor_type IN ('system', 'human', 'agent', 'executor')",
            name="ck_incident_events_actor_type",
        ),
        sa.CheckConstraint(
            "actor_type = 'system' OR actor_id IS NOT NULL",
            name="ck_incident_events_actor_id",
        ),
        sa.CheckConstraint(
            f"from_status IS NULL OR from_status IN ({INCIDENT_STATUS_SQL})",
            name="ck_incident_events_from_status",
        ),
        sa.CheckConstraint(
            f"to_status IN ({INCIDENT_STATUS_SQL})",
            name="ck_incident_events_to_status",
        ),
        sa.CheckConstraint(
            "(event_type = 'incident.status_changed' AND from_status IS NOT NULL "
            "AND from_status <> to_status) OR "
            "(event_type IN ('incident.created', 'incident.alert_attached') "
            "AND from_status IS NULL)",
            name="ck_incident_events_status_shape",
        ),
        sa.CheckConstraint(
            "event_type <> 'incident.created' OR (to_status = 'open' AND incident_version = 1)",
            name="ck_incident_events_creation_shape",
        ),
        sa.CheckConstraint("incident_version >= 1", name="ck_incident_events_version_positive"),
        sa.CheckConstraint("char_length(btrim(reason)) > 0", name="ck_incident_events_reason"),
        sa.CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_incident_events_payload_object",
        ),
        sa.ForeignKeyConstraint(["incident_id"], ["incidents.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_incident_events_incident_cursor",
        "incident_events",
        ["incident_id", "id"],
        unique=False,
    )
    op.create_index(
        "uq_incident_events_mutation_version",
        "incident_events",
        ["incident_id", "incident_version"],
        unique=True,
        postgresql_where=sa.text("event_type IN ('incident.created', 'incident.status_changed')"),
    )


def downgrade() -> None:
    """按外键依赖逆序删除第一批业务表。"""

    op.drop_index("uq_incident_events_mutation_version", table_name="incident_events")
    op.drop_index("ix_incident_events_incident_cursor", table_name="incident_events")
    op.drop_table("incident_events")
    op.drop_index("ix_alerts_incident_occurred_at", table_name="alerts")
    op.drop_table("alerts")
    op.drop_index("ix_incidents_status_created_at", table_name="incidents")
    op.drop_index("uq_incidents_active_fingerprint", table_name="incidents")
    op.drop_table("incidents")
