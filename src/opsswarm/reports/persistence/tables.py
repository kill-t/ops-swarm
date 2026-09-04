"""事故复盘报告不可变快照的 SQLAlchemy 映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class IncidentReportRow(Base):
    """一个已关闭 Incident 唯一、不可变的复盘报告。"""

    __tablename__ = "incident_reports"
    __table_args__ = (
        UniqueConstraint("incident_id", name="uq_incident_reports_incident"),
        CheckConstraint(
            "schema_version = 'incident-report.v1'",
            name="ck_incident_reports_schema_version",
        ),
        CheckConstraint("incident_version >= 1", name="ck_incident_reports_version_positive"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_incident_reports_content_hash",
        ),
        CheckConstraint(
            "jsonb_typeof(content) = 'object'",
            name="ck_incident_reports_content_object",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    incident_version: Mapped[int] = mapped_column(Integer, nullable=False)
    schema_version: Mapped[str] = mapped_column(String(50), nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
