"""Incident、Alert 与时间线事件的 SQLAlchemy 映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base

INCIDENT_STATUS_SQL = (
    "'open', 'investigating', 'awaiting_approval', 'mitigating', 'verifying', 'resolved', 'closed'"
)
SEVERITY_SQL = "'p1', 'p2', 'p3', 'p4'"
ACTOR_TYPE_SQL = "'system', 'human', 'agent', 'executor'"
EVENT_TYPE_SQL = "'incident.created', 'incident.status_changed', 'incident.alert_attached'"


class IncidentRow(Base):
    """Incident 当前状态的持久化快照。"""

    __tablename__ = "incidents"
    __table_args__ = (
        UniqueConstraint("display_number", name="uq_incidents_display_number"),
        CheckConstraint(f"severity IN ({SEVERITY_SQL})", name="ck_incidents_severity"),
        CheckConstraint(f"status IN ({INCIDENT_STATUS_SQL})", name="ck_incidents_status"),
        CheckConstraint("version >= 1", name="ck_incidents_version_positive"),
        CheckConstraint("created_at <= updated_at", name="ck_incidents_timestamp_order"),
        CheckConstraint(
            "((status IN ('resolved', 'closed')) AND resolved_at IS NOT NULL) OR "
            "((status NOT IN ('resolved', 'closed')) AND resolved_at IS NULL)",
            name="ck_incidents_resolved_time",
        ),
        CheckConstraint(
            "(status = 'closed' AND closed_at IS NOT NULL) OR "
            "(status <> 'closed' AND closed_at IS NULL)",
            name="ck_incidents_closed_time",
        ),
        CheckConstraint(
            "resolved_at IS NULL OR resolved_at <= updated_at",
            name="ck_incidents_resolved_before_update",
        ),
        CheckConstraint(
            "closed_at IS NULL OR closed_at <= updated_at",
            name="ck_incidents_closed_before_update",
        ),
        Index(
            "uq_incidents_active_fingerprint",
            "fingerprint",
            unique=True,
            postgresql_where=text("status <> 'closed'"),
        ),
        Index("ix_incidents_status_created_at", "status", text("created_at DESC")),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    display_number: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        nullable=False,
    )
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    severity: Mapped[str] = mapped_column(String(2), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(200), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AlertRow(Base):
    """接收到的原始告警及其外部幂等键。"""

    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "external_event_id",
            name="uq_alerts_source_external_event_id",
        ),
        CheckConstraint(f"severity IN ({SEVERITY_SQL})", name="ck_alerts_severity"),
        CheckConstraint("jsonb_typeof(payload) = 'object'", name="ck_alerts_payload_object"),
        Index("ix_alerts_incident_occurred_at", "incident_id", "occurred_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(200), nullable=False)
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(2), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IncidentEventRow(Base):
    """只追加的 Incident 时间线事件。"""

    __tablename__ = "incident_events"
    __table_args__ = (
        CheckConstraint(f"event_type IN ({EVENT_TYPE_SQL})", name="ck_incident_events_type"),
        CheckConstraint(f"actor_type IN ({ACTOR_TYPE_SQL})", name="ck_incident_events_actor_type"),
        CheckConstraint(
            "actor_type = 'system' OR actor_id IS NOT NULL",
            name="ck_incident_events_actor_id",
        ),
        CheckConstraint(
            f"from_status IS NULL OR from_status IN ({INCIDENT_STATUS_SQL})",
            name="ck_incident_events_from_status",
        ),
        CheckConstraint(
            f"to_status IN ({INCIDENT_STATUS_SQL})",
            name="ck_incident_events_to_status",
        ),
        CheckConstraint(
            "(event_type = 'incident.status_changed' AND from_status IS NOT NULL "
            "AND from_status <> to_status) OR "
            "(event_type IN ('incident.created', 'incident.alert_attached') "
            "AND from_status IS NULL)",
            name="ck_incident_events_status_shape",
        ),
        CheckConstraint(
            "event_type <> 'incident.created' OR (to_status = 'open' AND incident_version = 1)",
            name="ck_incident_events_creation_shape",
        ),
        CheckConstraint("incident_version >= 1", name="ck_incident_events_version_positive"),
        CheckConstraint("char_length(btrim(reason)) > 0", name="ck_incident_events_reason"),
        CheckConstraint(
            "jsonb_typeof(payload) = 'object'",
            name="ck_incident_events_payload_object",
        ),
        Index("ix_incident_events_incident_cursor", "incident_id", "id"),
        Index(
            "uq_incident_events_mutation_version",
            "incident_id",
            "incident_version",
            unique=True,
            postgresql_where=text("event_type IN ('incident.created', 'incident.status_changed')"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_type: Mapped[str] = mapped_column(String(20), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(100))
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    incident_version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=text("'{}'::jsonb"),
    )
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
