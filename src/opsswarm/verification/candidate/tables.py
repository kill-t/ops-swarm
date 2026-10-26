"""候选发布恢复验证的任务、独立尝试、逐次样本及不可变结果。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class RecoveryJobRow(Base):
    __tablename__ = "candidate_recovery_jobs"
    __table_args__ = (
        CheckConstraint("policy_version='candidate-recovery-v1'", name="ck_recovery_job_policy"),
        CheckConstraint(
            "verification_version BETWEEN 1 AND 8 AND attempt_count BETWEEN 0 AND 3",
            name="ck_recovery_job_counts",
        ),
        CheckConstraint(
            "status IN ('queued','running','completed')", name="ck_recovery_job_status"
        ),
        CheckConstraint("execution_result_sha256 ~ '^[0-9a-f]{64}$'", name="ck_recovery_job_hash"),
        CheckConstraint(
            "(status='running' AND locked_by IS NOT NULL AND lease_token IS NOT NULL AND "
            "locked_until IS NOT NULL AND completed_at IS NULL) OR (status<>'running' AND "
            "locked_by IS NULL AND lease_token IS NULL AND locked_until IS NULL AND "
            "((status='queued' AND completed_at IS NULL) OR "
            "(status='completed' AND completed_at IS NOT NULL)))",
            name="ck_recovery_job_lease",
        ),
        Index("ix_recovery_job_claim", "status", "locked_until", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_release_execution_jobs.id", ondelete="RESTRICT"), primary_key=True
    )
    investigation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_candidate_evaluation_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    verification_version: Mapped[int] = mapped_column(Integer, nullable=False)
    operation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    execution_result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    window_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    lease_token: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RecoveryAttemptRow(Base):
    __tablename__ = "candidate_recovery_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "number", name="uq_recovery_attempt_number"),
        UniqueConstraint("lease_token", name="uq_recovery_attempt_token"),
        CheckConstraint(
            "number BETWEEN 1 AND 3 AND lease_expires_at=started_at+interval '30 seconds'",
            name="ck_recovery_attempt_lease",
        ),
        CheckConstraint(
            "(status='running' AND finished_at IS NULL) OR "
            "(status IN ('succeeded','failed','lease_expired') AND finished_at IS NOT NULL "
            "AND finished_at>=started_at)",
            name="ck_recovery_attempt_status",
        ),
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_recovery_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class RecoverySampleRow(Base):
    __tablename__ = "candidate_recovery_samples"
    __table_args__ = (
        CheckConstraint("sequence BETWEEN 1 AND 3", name="ck_recovery_sample_sequence"),
        CheckConstraint(
            "(observation IS NOT NULL AND error_code IS NULL AND "
            "jsonb_typeof(observation)='object' AND octet_length(observation::text)<=4096) OR "
            "(observation IS NULL AND error_code IS NOT NULL AND char_length(error_code)>0)",
            name="ck_recovery_sample_content",
        ),
    )
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_recovery_attempts.id", ondelete="RESTRICT"), primary_key=True
    )
    sequence: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_recovery_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    observation: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class RecoveryResultRow(Base):
    __tablename__ = "candidate_recovery_results"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=8192 AND "
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_recovery_result_content",
        ),
    )
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_recovery_jobs.id", ondelete="RESTRICT"), primary_key=True
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
