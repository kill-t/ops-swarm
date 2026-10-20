"""经验生成任务、执行尝试及不可变候选的 PostgreSQL 映射。"""

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


class ExperienceJobRow(Base):
    __tablename__ = "experience_generation_jobs"
    __table_args__ = (
        UniqueConstraint("report_id", "policy_version", name="uq_experience_jobs_report_policy"),
        CheckConstraint(
            "policy_version = 'report-fact-draft-v1'", name="ck_experience_jobs_policy"
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed')",
            name="ck_experience_jobs_status",
        ),
        CheckConstraint(
            "max_attempts = 3 AND attempt_count BETWEEN 0 AND max_attempts",
            name="ck_experience_jobs_attempts",
        ),
        CheckConstraint("source_hash ~ '^[0-9a-f]{64}$'", name="ck_experience_jobs_hash"),
        CheckConstraint(
            "jsonb_typeof(source) = 'object' AND "
            "COALESCE(source->>'report_id' = report_id::text, false)",
            name="ck_experience_jobs_source",
        ),
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND locked_until IS NOT NULL) OR "
            "(status <> 'running' AND lease_token IS NULL AND locked_until IS NULL)",
            name="ck_experience_jobs_lease",
        ),
        CheckConstraint(
            "(status IN ('queued', 'running') AND completed_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)",
            name="ck_experience_jobs_completion",
        ),
        CheckConstraint(
            "status <> 'failed' OR last_error_code IS NOT NULL", name="ck_experience_jobs_error"
        ),
        CheckConstraint(
            "created_at <= updated_at AND available_at >= created_at AND "
            "(completed_at IS NULL OR completed_at >= created_at)",
            name="ck_experience_jobs_time",
        ),
        Index("ix_experience_jobs_claim", "status", "available_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    report_id: Mapped[UUID] = mapped_column(
        ForeignKey("incident_reports.id", ondelete="RESTRICT"), nullable=False
    )
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_token: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExperienceAttemptRow(Base):
    __tablename__ = "experience_generation_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "number", name="uq_experience_attempts_number"),
        CheckConstraint("number BETWEEN 1 AND 3", name="ck_experience_attempts_number"),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed', 'lease_expired')",
            name="ck_experience_attempts_status",
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status IN ('failed', 'lease_expired') AND finished_at IS NOT NULL "
            "AND error_code IS NOT NULL)",
            name="ck_experience_attempts_completion",
        ),
        CheckConstraint(
            "lease_expires_at > started_at AND (finished_at IS NULL OR finished_at >= started_at)",
            name="ck_experience_attempts_time",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("experience_generation_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    worker_id: Mapped[str] = mapped_column(String(100), nullable=False)
    lease_token: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(80))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ExperienceCandidateRow(Base):
    __tablename__ = "experience_candidates"
    __table_args__ = (
        CheckConstraint(
            "version = 1 AND status = 'pending_review'",
            name="ck_experience_candidates_version_status",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$' AND source_hash ~ '^[0-9a-f]{64}$'",
            name="ck_experience_candidates_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(content) = 'object'", name="ck_experience_candidates_content"
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("experience_generation_jobs.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    source_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
