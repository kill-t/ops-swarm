"""复盘改进行动项与追加式事件的 SQLAlchemy 映射。"""

from datetime import datetime
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
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class ReportActionItemRow(Base):
    """事故复盘报告下的当前行动项状态。"""

    __tablename__ = "report_action_items"
    __table_args__ = (
        UniqueConstraint(
            "report_id",
            "idempotency_key",
            name="uq_report_action_items_report_idempotency",
        ),
        CheckConstraint(
            "status IN ('open', 'completed')",
            name="ck_report_action_items_status",
        ),
        CheckConstraint("version >= 1", name="ck_report_action_items_version_positive"),
        CheckConstraint("due_at > created_at", name="ck_report_action_items_due_time"),
        CheckConstraint(
            "char_length(btrim(title)) BETWEEN 1 AND 200 AND "
            "char_length(btrim(description)) BETWEEN 1 AND 1000 AND "
            "char_length(btrim(owner_id)) BETWEEN 1 AND 100 AND "
            "char_length(btrim(created_by_id)) BETWEEN 1 AND 100 AND "
            "(completed_by_id IS NULL OR "
            "char_length(btrim(completed_by_id)) BETWEEN 1 AND 100) AND "
            "(completion_note IS NULL OR "
            "char_length(btrim(completion_note)) BETWEEN 1 AND 500)",
            name="ck_report_action_items_text",
        ),
        CheckConstraint(
            "(status = 'open' AND version = 1 AND completed_by_id IS NULL "
            "AND completion_note IS NULL AND completed_at IS NULL) OR "
            "(status = 'completed' AND version = 2 AND completed_by_id IS NOT NULL "
            "AND completion_note IS NOT NULL AND completed_at IS NOT NULL)",
            name="ck_report_action_items_lifecycle",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_report_action_items_completion_time",
        ),
        Index(
            "ix_report_action_items_report_status_due",
            "report_id",
            "status",
            "due_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    report_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("incident_reports.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(String(1000), nullable=False)
    owner_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_by_id: Mapped[str | None] = mapped_column(String(100))
    completion_note: Mapped[str | None] = mapped_column(String(500))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ReportActionItemEventRow(Base):
    """行动项创建和完成的不可变审计事件。"""

    __tablename__ = "report_action_item_events"
    __table_args__ = (
        UniqueConstraint(
            "action_item_id",
            "item_version",
            name="uq_report_action_item_events_version",
        ),
        CheckConstraint(
            "event_type IN ('report_action_item.created', 'report_action_item.completed')",
            name="ck_report_action_item_events_type",
        ),
        CheckConstraint(
            "(event_type = 'report_action_item.created' AND from_status IS NULL "
            "AND to_status = 'open' AND item_version = 1) OR "
            "(event_type = 'report_action_item.completed' AND from_status = 'open' "
            "AND to_status = 'completed' AND item_version = 2)",
            name="ck_report_action_item_events_shape",
        ),
        CheckConstraint(
            "char_length(btrim(actor_id)) BETWEEN 1 AND 100 AND "
            "char_length(btrim(reason)) BETWEEN 1 AND 500",
            name="ck_report_action_item_events_text",
        ),
        Index(
            "ix_report_action_item_events_item_cursor",
            "action_item_id",
            "id",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    action_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("report_action_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(100), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32), nullable=False)
    reason: Mapped[str] = mapped_column(String(500), nullable=False)
    item_version: Mapped[int] = mapped_column(Integer, nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
