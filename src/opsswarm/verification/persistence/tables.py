"""恢复验证运行与样本的 SQLAlchemy 映射。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class VerificationRunRow(Base):
    """冻结恢复规则并汇总样本进度的一次验证运行。"""

    __tablename__ = "verification_runs"
    __table_args__ = (
        CheckConstraint(
            "status IN ('running', 'passed', 'failed')",
            name="ck_verification_runs_status",
        ),
        CheckConstraint(
            "verifier_name IN ('local_order_http_5xx_ratio_v1')",
            name="ck_verification_runs_verifier",
        ),
        CheckConstraint(
            "signal = 'http_5xx_ratio' AND operator = 'less_than'",
            name="ck_verification_runs_rule_kind",
        ),
        CheckConstraint(
            "threshold >= 0 AND threshold <= 1",
            name="ck_verification_runs_threshold",
        ),
        CheckConstraint(
            "observation_window_seconds BETWEEN 10 AND 3600",
            name="ck_verification_runs_window",
        ),
        CheckConstraint(
            "required_successful_samples BETWEEN 1 AND 100",
            name="ck_verification_runs_required_samples",
        ),
        CheckConstraint(
            "successful_sample_count >= 0",
            name="ck_verification_runs_sample_count",
        ),
        CheckConstraint(
            "(successful_sample_count = 0 AND first_success_at IS NULL) OR "
            "(successful_sample_count > 0 AND first_success_at IS NOT NULL)",
            name="ck_verification_runs_first_success",
        ),
        CheckConstraint(
            "last_observed_at IS NULL OR last_observed_at >= created_at",
            name="ck_verification_runs_last_observed",
        ),
        CheckConstraint(
            "(status = 'running' AND completed_at IS NULL AND failure_code IS NULL "
            "AND failure_message IS NULL) OR "
            "(status = 'passed' AND completed_at IS NOT NULL AND failure_code IS NULL "
            "AND failure_message IS NULL AND successful_sample_count >= "
            "required_successful_samples AND first_success_at IS NOT NULL "
            "AND last_observed_at IS NOT NULL) OR "
            "(status = 'failed' AND completed_at IS NOT NULL AND failure_code IS NOT NULL "
            "AND failure_message IS NOT NULL)",
            name="ck_verification_runs_lifecycle",
        ),
        CheckConstraint(
            "status <> 'passed' OR EXTRACT(EPOCH FROM "
            "(last_observed_at - first_success_at)) >= observation_window_seconds",
            name="ck_verification_runs_passed_window",
        ),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_verification_runs_completion_time",
        ),
        ForeignKeyConstraint(
            ["operation_execution_id", "incident_id", "action_step_id"],
            [
                "operation_executions.id",
                "operation_executions.incident_id",
                "operation_executions.action_step_id",
            ],
            ondelete="RESTRICT",
            name="fk_verification_runs_exact_execution",
        ),
        UniqueConstraint(
            "operation_execution_id",
            name="uq_verification_runs_operation_execution",
        ),
        Index(
            "ix_verification_runs_incident_status",
            "incident_id",
            "status",
            "created_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    operation_execution_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
    )
    incident_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    action_step_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    verifier_name: Mapped[str] = mapped_column(String(100), nullable=False)
    incident_version_before: Mapped[int] = mapped_column(Integer, nullable=False)
    signal: Mapped[str] = mapped_column(String(100), nullable=False)
    operator: Mapped[str] = mapped_column(String(32), nullable=False)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    observation_window_seconds: Mapped[int] = mapped_column(Integer, nullable=False)
    required_successful_samples: Mapped[int] = mapped_column(Integer, nullable=False)
    successful_sample_count: Mapped[int] = mapped_column(Integer, nullable=False)
    first_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    failure_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class VerificationSampleRow(Base):
    """一个只追加的数值样本或探针错误。"""

    __tablename__ = "verification_samples"
    __table_args__ = (
        CheckConstraint(
            "status IN ('passed', 'failed', 'error')",
            name="ck_verification_samples_status",
        ),
        CheckConstraint("sequence >= 1", name="ck_verification_samples_sequence"),
        CheckConstraint(
            "signal = 'http_5xx_ratio'",
            name="ck_verification_samples_signal",
        ),
        CheckConstraint(
            "threshold >= 0 AND threshold <= 1",
            name="ck_verification_samples_threshold",
        ),
        CheckConstraint(
            "observed_value IS NULL OR (observed_value >= 0 AND observed_value <= 1)",
            name="ck_verification_samples_value",
        ),
        CheckConstraint(
            "http_status IS NULL OR http_status BETWEEN 100 AND 599",
            name="ck_verification_samples_http_status",
        ),
        CheckConstraint(
            "(status = 'passed' AND observed_value IS NOT NULL AND "
            "observed_value < threshold AND http_status IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL) OR "
            "(status = 'failed' AND observed_value IS NOT NULL AND "
            "observed_value >= threshold AND http_status IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL) OR "
            "(status = 'error' AND observed_value IS NULL AND error_code IS NOT NULL "
            "AND error_message IS NOT NULL)",
            name="ck_verification_samples_outcome",
        ),
        UniqueConstraint(
            "verification_run_id",
            "idempotency_key",
            name="uq_verification_samples_run_idempotency",
        ),
        UniqueConstraint(
            "verification_run_id",
            "sequence",
            name="uq_verification_samples_run_sequence",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    verification_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("verification_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    signal: Mapped[str] = mapped_column(String(100), nullable=False)
    observed_value: Mapped[float | None] = mapped_column(Float)
    threshold: Mapped[float] = mapped_column(Float, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(500))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
