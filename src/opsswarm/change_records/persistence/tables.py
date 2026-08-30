"""变更记录 SQLAlchemy 映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, Index, String, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class ChangeRecordRow(Base):
    """只追加、不提供更新与删除接口的外部变更事实。"""

    __tablename__ = "change_records"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "external_change_id",
            name="uq_change_records_source_external_id",
        ),
        CheckConstraint(
            "source = 'fault-lab-change-simulator'",
            name="ck_change_records_source",
        ),
        CheckConstraint(
            "service_name = 'order-service'",
            name="ck_change_records_service",
        ),
        CheckConstraint(
            "change_type IN ('deployment', 'configuration', 'dependency_maintenance')",
            name="ck_change_records_type",
        ),
        CheckConstraint("jsonb_typeof(details) = 'object'", name="ck_change_records_details"),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_change_records_content_hash",
        ),
        CheckConstraint(
            "occurred_at <= recorded_at",
            name="ck_change_records_timestamp_order",
        ),
        Index(
            "ix_change_records_service_occurred",
            "service_name",
            text("occurred_at DESC"),
            text("id DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    external_change_id: Mapped[str] = mapped_column(String(100), nullable=False)
    service_name: Mapped[str] = mapped_column(String(100), nullable=False)
    change_type: Mapped[str] = mapped_column(String(32), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
