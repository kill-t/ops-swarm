"""回流综合的冻结输入、租约尝试及不可变待审查提案。"""

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
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class SynthesisJobRow(Base):
    __tablename__ = "investigation_reflow_synthesis_jobs"
    __table_args__ = (
        UniqueConstraint("id", "investigation_run_id", name="uq_reflow_synthesis_jobs_id_run"),
        CheckConstraint("id=investigation_run_id", name="ck_reflow_synthesis_jobs_identity"),
        CheckConstraint(
            "policy_version='reinvestigation-synthesis-v1'", name="ck_reflow_synthesis_jobs_policy"
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','blocked','failed') AND "
            "attempt_count BETWEEN 0 AND 3",
            name="ck_reflow_synthesis_jobs_state",
        ),
        CheckConstraint(
            "(source IS NULL)=(source_sha256 IS NULL) AND (packet IS NULL)=(packet_sha256"
            " IS NULL) AND (packet IS NULL OR source IS NOT NULL) AND (status NOT IN "
            "('queued','running','succeeded') OR (source IS NOT NULL AND packet IS NOT "
            "NULL))",
            name="ck_reflow_synthesis_jobs_material_presence",
        ),
        CheckConstraint(
            "(source IS NULL OR (jsonb_typeof(source)='object' AND "
            "octet_length(source::text)<=1048576 AND source_sha256 ~ '^[0-9a-f]{64}$')) "
            "AND (packet IS NULL OR (jsonb_typeof(packet)='object' AND "
            "octet_length(packet::text)<=1048576 AND packet_sha256 ~ '^[0-9a-f]{64}$'))",
            name="ck_reflow_synthesis_jobs_material",
        ),
        CheckConstraint(
            "(status='running' AND locked_by IS NOT NULL AND lease_token IS NOT NULL AND "
            "locked_until IS NOT NULL) OR (status<>'running' AND locked_by IS NULL AND "
            "lease_token IS NULL AND locked_until IS NULL)",
            name="ck_reflow_synthesis_jobs_lease",
        ),
        CheckConstraint(
            "(status IN ('queued','running') AND completed_at IS NULL) OR (status IN "
            "('succeeded','blocked','failed') AND completed_at IS NOT NULL)",
            name="ck_reflow_synthesis_jobs_completion",
        ),
        CheckConstraint(
            "status NOT IN ('blocked','failed') OR last_error IS NOT NULL",
            name="ck_reflow_synthesis_jobs_error",
        ),
        CheckConstraint(
            "created_at<=updated_at AND available_at>=created_at AND (completed_at IS "
            "NULL OR completed_at>=created_at)",
            name="ck_reflow_synthesis_jobs_time",
        ),
        Index("ix_reflow_synthesis_jobs_claim", "status", "available_at", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey(
            "candidate_reinvestigation_admissions.investigation_run_id", ondelete="RESTRICT"
        ),
        unique=True,
        nullable=False,
    )
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    source_sha256: Mapped[str | None] = mapped_column(String(64))
    packet: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    packet_sha256: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(100))
    lease_token: Mapped[UUID | None] = mapped_column()
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(String(100))


class SynthesisAttemptRow(Base):
    __tablename__ = "investigation_reflow_synthesis_attempts"
    __table_args__ = (
        UniqueConstraint("id", "investigation_run_id", name="uq_reflow_synthesis_attempts_id_run"),
        UniqueConstraint("id", "job_id", name="uq_reflow_synthesis_attempts_id_job"),
        UniqueConstraint("job_id", "number", name="uq_reflow_synthesis_attempts_number"),
        UniqueConstraint("lease_token", name="uq_reflow_synthesis_attempts_token"),
        ForeignKeyConstraint(
            ["job_id", "investigation_run_id"],
            [
                "investigation_reflow_synthesis_jobs.id",
                "investigation_reflow_synthesis_jobs.investigation_run_id",
            ],
            name="fk_reflow_synthesis_attempts_job",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "number BETWEEN 1 AND 3 AND status IN ('running','succeeded','failed','lease_expired')",
            name="ck_reflow_synthesis_attempts_state",
        ),
        CheckConstraint(
            "(status='running' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status='succeeded' AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status IN ('failed','lease_expired') AND finished_at IS NOT NULL AND "
            "error_code IS NOT NULL)",
            name="ck_reflow_synthesis_attempts_completion",
        ),
        CheckConstraint(
            "lease_expires_at>started_at AND (finished_at IS NULL OR finished_at>=started_at)",
            name="ck_reflow_synthesis_attempts_time",
        ),
    )
    id: Mapped[UUID] = mapped_column(primary_key=True)
    job_id: Mapped[UUID] = mapped_column(nullable=False)
    investigation_run_id: Mapped[UUID] = mapped_column(nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[UUID] = mapped_column(nullable=False)
    worker_id: Mapped[str] = mapped_column(String(100), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))


class SynthesisResultRow(Base):
    __tablename__ = "investigation_reflow_synthesis_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["job_id", "investigation_run_id"],
            [
                "investigation_reflow_synthesis_jobs.id",
                "investigation_reflow_synthesis_jobs.investigation_run_id",
            ],
            name="fk_reflow_synthesis_results_job",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["attempt_id", "job_id"],
            [
                "investigation_reflow_synthesis_attempts.id",
                "investigation_reflow_synthesis_attempts.job_id",
            ],
            name="fk_reflow_synthesis_results_attempt",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["source_call_id", "investigation_run_id"],
            [
                "investigation_call_reservations.id",
                "investigation_call_reservations.investigation_run_id",
            ],
            name="fk_reflow_synthesis_results_call",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=65536 AND "
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reflow_synthesis_results_content",
        ),
    )
    job_id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(nullable=False)
    attempt_id: Mapped[UUID] = mapped_column(nullable=False)
    source_call_id: Mapped[UUID] = mapped_column(nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
