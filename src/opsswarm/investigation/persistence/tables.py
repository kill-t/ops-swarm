"""调查运行与 PostgreSQL 租约任务映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Index, Integer, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base

RUN_STATUS_SQL = "'queued', 'running', 'succeeded', 'failed'"
WORK_STATUS_SQL = "'pending', 'running', 'completed', 'failed'"


class InvestigationRunRow(Base):
    """一次调查的业务状态与最终结构化结果。"""

    __tablename__ = "investigation_runs"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('single_agent', 'multi_agent')",
            name="ck_investigation_runs_mode",
        ),
        CheckConstraint(
            "mode <> 'multi_agent' OR graph_thread_id IS NOT NULL",
            name="ck_investigation_runs_multi_thread",
        ),
        CheckConstraint(f"status IN ({RUN_STATUS_SQL})", name="ck_investigation_runs_status"),
        CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'",
            name="ck_investigation_runs_result_object",
        ),
        CheckConstraint(
            "metrics IS NULL OR jsonb_typeof(metrics) = 'object'",
            name="ck_investigation_runs_metrics_object",
        ),
        CheckConstraint(
            "last_error IS NULL OR jsonb_typeof(last_error) = 'object'",
            name="ck_investigation_runs_error_object",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND result IS NOT NULL) OR "
            "(status <> 'succeeded' AND result IS NULL)",
            name="ck_investigation_runs_result_shape",
        ),
        CheckConstraint(
            "metrics IS NULL OR status = 'succeeded'",
            name="ck_investigation_runs_metrics_shape",
        ),
        CheckConstraint(
            "status <> 'failed' OR last_error IS NOT NULL",
            name="ck_investigation_runs_failure_shape",
        ),
        CheckConstraint(
            "(status IN ('queued', 'running') AND completed_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)",
            name="ck_investigation_runs_completion_shape",
        ),
        CheckConstraint(
            "status = 'queued' OR started_at IS NOT NULL",
            name="ck_investigation_runs_started_shape",
        ),
        CheckConstraint("created_at <= updated_at", name="ck_investigation_runs_time_order"),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_investigation_runs_completed_time",
        ),
        Index(
            "uq_investigation_runs_active_incident",
            "incident_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_investigation_runs_incident_created", "incident_id", text("created_at DESC")),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    graph_thread_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    model_name: Mapped[str | None] = mapped_column(String(200))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkItemRow(Base):
    """由 Worker 使用租约领取的至少一次处理任务。"""

    __tablename__ = "work_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('run_single_agent_investigation', 'run_multi_agent_investigation')",
            name="ck_work_items_kind",
        ),
        CheckConstraint(f"status IN ({WORK_STATUS_SQL})", name="ck_work_items_status"),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts",
            name="ck_work_items_attempts",
        ),
        CheckConstraint(
            "last_error IS NULL OR jsonb_typeof(last_error) = 'object'",
            name="ck_work_items_error_object",
        ),
        CheckConstraint(
            "(status = 'running' AND locked_by IS NOT NULL AND locked_until IS NOT NULL "
            "AND lease_token IS NOT NULL) OR "
            "(status <> 'running' AND locked_by IS NULL AND locked_until IS NULL "
            "AND lease_token IS NULL)",
            name="ck_work_items_lease_shape",
        ),
        CheckConstraint(
            "(status IN ('pending', 'running') AND completed_at IS NULL) OR "
            "(status IN ('completed', 'failed') AND completed_at IS NOT NULL)",
            name="ck_work_items_completion_shape",
        ),
        CheckConstraint(
            "status <> 'failed' OR last_error IS NOT NULL",
            name="ck_work_items_failure_shape",
        ),
        CheckConstraint("created_at <= updated_at", name="ck_work_items_time_order"),
        Index(
            "ix_work_items_pending_claim",
            "available_at",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_work_items_expired_lease",
            "locked_until",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
