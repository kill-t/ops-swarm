"""审批请求 SQLAlchemy 映射。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class ApprovalRequestRow(Base):
    """等待人工决定且有明确过期时间的审批请求。"""

    __tablename__ = "approval_requests"
    __table_args__ = (
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected', 'expired')",
            name="ck_approval_requests_status",
        ),
        CheckConstraint(
            "requested_by_type IN ('human')",
            name="ck_approval_requests_requester_type",
        ),
        CheckConstraint(
            "plan_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_approval_requests_content_hash",
        ),
        CheckConstraint(
            "expires_at > created_at",
            name="ck_approval_requests_expiry",
        ),
        CheckConstraint(
            "(status = 'pending' AND decided_by_type IS NULL "
            "AND decided_by_id IS NULL AND decision_reason IS NULL AND decided_at IS NULL) "
            "OR (status IN ('approved', 'rejected') AND decided_by_type = 'human' "
            "AND decided_by_id IS NOT NULL AND decision_reason IS NOT NULL "
            "AND decided_at IS NOT NULL) "
            "OR (status = 'expired' AND decided_by_type = 'system' "
            "AND decided_by_id IS NULL AND decision_reason IS NOT NULL "
            "AND decided_at IS NOT NULL)",
            name="ck_approval_requests_decision_audit",
        ),
        CheckConstraint(
            "decided_at IS NULL OR decided_at >= created_at",
            name="ck_approval_requests_decision_time",
        ),
        ForeignKeyConstraint(
            ["action_plan_id", "action_plan_version"],
            ["action_plans.id", "action_plans.version"],
            ondelete="RESTRICT",
            name="fk_approval_requests_exact_plan_version",
        ),
        UniqueConstraint(
            "action_plan_id",
            name="uq_approval_requests_action_plan",
        ),
        Index(
            "ix_approval_requests_status_expires",
            "status",
            "expires_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    action_plan_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    action_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    plan_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    requested_by_type: Mapped[str] = mapped_column(String(20), nullable=False)
    requested_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    request_reason: Mapped[str] = mapped_column(String(500), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    decided_by_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    decided_by_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    decision_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
