"""人工重评准备任务、独立尝试、不可变输入工件及命令审计。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Identity,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class HumanPreparationJobRow(Base):
    __tablename__ = "investigation_human_preparation_jobs"
    __table_args__ = (
        UniqueConstraint(
            "investigation_run_id", "wait_version", name="uq_human_preparation_jobs_version"
        ),
        UniqueConstraint(
            "investigation_run_id",
            "requested_by_id",
            "idempotency_key",
            name="uq_human_preparation_jobs_key",
        ),
        UniqueConstraint("authorization_id", name="uq_human_preparation_jobs_authorization"),
        ForeignKeyConstraint(
            ["handoff_id", "investigation_run_id"],
            [
                "investigation_terminal_handoffs.id",
                "investigation_terminal_handoffs.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_human_preparation_jobs_handoff",
        ),
        CheckConstraint("wait_version BETWEEN 2 AND 16", name="ck_human_preparation_jobs_version"),
        CheckConstraint(
            "policy_version='human-input-preparation-v1'", name="ck_human_preparation_jobs_policy"
        ),
        CheckConstraint(
            "jsonb_typeof(source)='object' AND octet_length(source::text)<=524288",
            name="ck_human_preparation_jobs_source",
        ),
        CheckConstraint(
            "source_sha256 ~ '^[0-9a-f]{64}$' AND command_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_human_preparation_jobs_hashes",
        ),
        CheckConstraint(
            "char_length(btrim(requested_by_id)) BETWEEN 1 AND 100",
            name="ck_human_preparation_jobs_subject",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 AND idempotency_key ~ "
            "'^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_human_preparation_jobs_key",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed')",
            name="ck_human_preparation_jobs_status",
        ),
        CheckConstraint("attempt_count BETWEEN 0 AND 3", name="ck_human_preparation_jobs_attempts"),
        CheckConstraint(
            "(status='running' AND locked_by IS NOT NULL AND lease_token IS NOT NULL AND "
            "locked_until IS NOT NULL) OR (status<>'running' AND locked_by IS NULL AND "
            "lease_token IS NULL AND locked_until IS NULL)",
            name="ck_human_preparation_jobs_lease",
        ),
        CheckConstraint(
            "(status IN ('queued','running') AND completed_at IS NULL) OR (status IN "
            "('succeeded','failed') AND completed_at IS NOT NULL)",
            name="ck_human_preparation_jobs_completion",
        ),
        CheckConstraint(
            "status<>'failed' OR last_error_code IS NOT NULL",
            name="ck_human_preparation_jobs_error",
        ),
        CheckConstraint(
            "created_at<=updated_at AND available_at>=created_at AND (completed_at IS NULL OR "
            "completed_at>=created_at)",
            name="ck_human_preparation_jobs_time",
        ),
        Index("ix_human_preparation_jobs_claim", "status", "available_at", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    handoff_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    wait_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    requested_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    lease_token: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class HumanPreparationAttemptRow(Base):
    __tablename__ = "investigation_human_preparation_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "number", name="uq_human_preparation_attempts_number"),
        UniqueConstraint("id", "job_id", name="uq_human_preparation_attempts_id_job"),
        UniqueConstraint("lease_token", name="uq_human_preparation_attempts_token"),
        CheckConstraint("number BETWEEN 1 AND 3", name="ck_human_preparation_attempts_number"),
        CheckConstraint(
            "status IN ('running','succeeded','failed','lease_expired')",
            name="ck_human_preparation_attempts_status",
        ),
        CheckConstraint(
            "(status='running' AND finished_at IS NULL AND error_code IS NULL) OR "
            "(status='succeeded' AND finished_at IS NOT NULL AND error_code IS NULL) OR "
            "(status IN ('failed','lease_expired') AND finished_at IS NOT NULL AND error_code "
            "IS NOT NULL)",
            name="ck_human_preparation_attempts_completion",
        ),
        CheckConstraint(
            "lease_expires_at>started_at AND (finished_at IS NULL OR finished_at>=started_at)",
            name="ck_human_preparation_attempts_time",
        ),
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_human_preparation_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))


class HumanPreparationArtifactRow(Base):
    __tablename__ = "investigation_human_preparation_artifacts"
    __table_args__ = (
        ForeignKeyConstraint(
            ["attempt_id", "job_id"],
            [
                "investigation_human_preparation_attempts.id",
                "investigation_human_preparation_attempts.job_id",
            ],
            ondelete="RESTRICT",
            name="fk_human_preparation_artifacts_attempt",
        ),
        CheckConstraint(
            "version='human-preparation-artifact-v1'", name="ck_human_preparation_artifacts_version"
        ),
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=1048576",
            name="ck_human_preparation_artifacts_content",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_human_preparation_artifacts_hash"
        ),
    )
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_human_preparation_jobs.id", ondelete="RESTRICT"), primary_key=True
    )
    attempt_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HumanPreparationCommandRow(Base):
    __tablename__ = "investigation_human_preparation_commands"
    __table_args__ = (
        UniqueConstraint("authorization_id", name="uq_human_preparation_commands_authorization"),
        CheckConstraint(
            "jsonb_typeof(command)='object' AND octet_length(command::text)<=32768",
            name="ck_human_preparation_commands_body",
        ),
        CheckConstraint(
            "octet_length(raw_body) BETWEEN 2 AND 65536", name="ck_human_preparation_commands_raw"
        ),
        CheckConstraint(
            "command_sha256 ~ '^[0-9a-f]{64}$'", name="ck_human_preparation_commands_hash"
        ),
        CheckConstraint(
            "char_length(btrim(subject_id)) BETWEEN 1 AND 100",
            name="ck_human_preparation_commands_subject",
        ),
        CheckConstraint(
            "(outcome IN ('applied','replayed') AND job_id IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL AND ((outcome='applied' AND http_status=201) OR "
            "(outcome='replayed' AND http_status=200))) OR (outcome='error' AND job_id IS NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL AND http_status IN "
            "(404,409))",
            name="ck_human_preparation_commands_outcome",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$'",
            name="ck_human_preparation_commands_error",
        ),
        Index("ix_human_preparation_commands_run_cursor", "investigation_run_id", "cursor"),
    )
    cursor: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False)
    command: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("investigation_human_preparation_jobs.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
