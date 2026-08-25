"""操作执行 SQLAlchemy 映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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


class OperationExecutionRow(Base):
    """带租约、幂等键和有限结果的一次白名单操作。"""

    __tablename__ = "operation_executions"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_operation_executions_status",
        ),
        CheckConstraint("attempt_count >= 1", name="ck_operation_executions_attempt_count"),
        CheckConstraint(
            "executor_name IN ('local_fault_lab_release_500_v1')",
            name="ck_operation_executions_executor",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND locked_until IS NOT NULL "
            "AND result IS NULL AND error_code IS NULL AND error_message IS NULL "
            "AND completed_at IS NULL) OR "
            "(status = 'succeeded' AND lease_token IS NULL AND locked_until IS NULL "
            "AND jsonb_typeof(result) = 'object' AND error_code IS NULL "
            "AND error_message IS NULL AND completed_at IS NOT NULL) OR "
            "(status = 'failed' AND lease_token IS NULL AND locked_until IS NULL "
            "AND result IS NULL AND error_code IS NOT NULL AND error_message IS NOT NULL "
            "AND completed_at IS NOT NULL)",
            name="ck_operation_executions_lifecycle",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_operation_executions_completion_time",
        ),
        ForeignKeyConstraint(
            ["approval_request_id", "action_plan_id", "action_plan_version"],
            [
                "approval_requests.id",
                "approval_requests.action_plan_id",
                "approval_requests.action_plan_version",
            ],
            ondelete="RESTRICT",
            name="fk_operation_executions_exact_approval",
        ),
        ForeignKeyConstraint(
            ["action_plan_id", "action_plan_version"],
            ["action_plans.id", "action_plans.version"],
            ondelete="RESTRICT",
            name="fk_operation_executions_exact_plan",
        ),
        ForeignKeyConstraint(
            ["action_step_id", "action_plan_id"],
            ["action_steps.id", "action_steps.action_plan_id"],
            ondelete="RESTRICT",
            name="fk_operation_executions_step_plan",
        ),
        UniqueConstraint("idempotency_key", name="uq_operation_executions_idempotency_key"),
        UniqueConstraint(
            "id",
            "incident_id",
            "action_step_id",
            name="uq_operation_executions_verification_binding",
        ),
        Index(
            "uq_operation_executions_running_step",
            "action_step_id",
            unique=True,
            postgresql_where=text("status = 'running'"),
        ),
        Index(
            "uq_operation_executions_succeeded_step",
            "action_step_id",
            unique=True,
            postgresql_where=text("status = 'succeeded'"),
        ),
        Index(
            "ix_operation_executions_incident_status",
            "incident_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    incident_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    approval_request_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    action_plan_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    action_plan_version: Mapped[int] = mapped_column(Integer, nullable=False)
    action_step_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    executor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    incident_version_before: Mapped[int] = mapped_column(Integer, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=True)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # 可空结果必须写成 SQL NULL，不能让 JSONB 把 Python None 编码成 JSON null。
    result: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
