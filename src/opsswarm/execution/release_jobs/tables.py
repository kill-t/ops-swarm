"""候选发布任务、租约尝试、业务命令与不可变执行结果。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
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


class ReleaseExecutionJobRow(Base):
    __tablename__ = "candidate_release_execution_jobs"
    __table_args__ = (
        UniqueConstraint("request_id", name="uq_release_job_request"),
        UniqueConstraint("operation_id", name="uq_release_job_operation"),
        UniqueConstraint("authorization_id", name="uq_release_job_authorization"),
        UniqueConstraint(
            "investigation_run_id", "requested_by_id", "idempotency_key", name="uq_release_job_key"
        ),
        CheckConstraint(
            "policy_version='candidate-release-execution-v1'", name="ck_release_job_policy"
        ),
        CheckConstraint(
            "verification_version BETWEEN 1 AND 8 AND attempt_count BETWEEN 0 AND 3",
            name="ck_release_job_counts",
        ),
        CheckConstraint(
            "status IN ('queued','running','succeeded','failed','unknown')",
            name="ck_release_job_status",
        ),
        CheckConstraint(
            "(phase='prepared' AND dispatched_at IS NULL AND dispatch_attempt_id IS NULL) "
            "OR (phase='dispatched' AND dispatched_at IS NOT NULL AND dispatch_attempt_id "
            "IS NOT NULL)",
            name="ck_release_job_phase",
        ),
        CheckConstraint(
            "(status='running' AND locked_by IS NOT NULL AND lease_token IS NOT NULL AND "
            "locked_until IS NOT NULL AND completed_at IS NULL) OR (status<>'running' AND "
            "locked_by IS NULL AND lease_token IS NULL AND locked_until IS NULL AND "
            "((status='queued' AND completed_at IS NULL) OR (status IN "
            "('succeeded','failed','unknown') AND completed_at IS NOT NULL)))",
            name="ck_release_job_lease",
        ),
        CheckConstraint(
            "jsonb_typeof(dispatch_command)='object' AND "
            "octet_length(dispatch_command::text)<=8192",
            name="ck_release_job_dispatch",
        ),
        CheckConstraint(
            "request_content_sha256 ~ '^[0-9a-f]{64}$' AND credential_binding_sha256 ~ "
            "'^[0-9a-f]{64}$' AND command_sha256 ~ '^[0-9a-f]{64}$' AND dispatch_sha256 ~ "
            "'^[0-9a-f]{64}$'",
            name="ck_release_job_hashes",
        ),
        Index("ix_release_job_claim", "status", "locked_until", "created_at"),
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"), nullable=False
    )
    candidate_job_id: Mapped[UUID] = mapped_column(
        ForeignKey("investigation_candidate_evaluation_jobs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    verification_version: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_release_execution_requests.id", ondelete="RESTRICT"), nullable=False
    )
    request_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    requested_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    credential_id: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_binding_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    dispatch_command: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    dispatch_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    phase: Mapped[str] = mapped_column(String(16), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    lease_token: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatch_attempt_id: Mapped[UUID | None] = mapped_column(
        ForeignKey(
            "candidate_release_execution_attempts.id",
            name="fk_release_job_dispatch_attempt",
            use_alter=True,
            ondelete="RESTRICT",
        )
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))


class ReleaseExecutionAttemptRow(Base):
    __tablename__ = "candidate_release_execution_attempts"
    __table_args__ = (
        UniqueConstraint("job_id", "number", name="uq_release_attempt_number"),
        UniqueConstraint("lease_token", name="uq_release_attempt_token"),
        CheckConstraint(
            "number BETWEEN 1 AND 3 AND started_at<lease_expires_at",
            name="ck_release_attempt_lease",
        ),
        CheckConstraint(
            "(status='running' AND finished_at IS NULL AND error_code IS NULL) OR (status "
            "IN ('succeeded','failed','lease_expired') AND finished_at IS NOT NULL AND "
            "finished_at>=started_at)",
            name="ck_release_attempt_status",
        ),
    )
    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_release_execution_jobs.id", ondelete="RESTRICT"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))


class ReleaseExecutionResultRow(Base):
    __tablename__ = "candidate_release_execution_results"
    __table_args__ = (
        UniqueConstraint("attempt_id", name="uq_release_result_attempt"),
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=65536 AND "
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_release_result_content",
        ),
    )
    job_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_release_execution_jobs.id", ondelete="RESTRICT"), primary_key=True
    )
    attempt_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_release_execution_attempts.id", ondelete="RESTRICT"), nullable=False
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReleaseExecutionCommandRow(Base):
    __tablename__ = "candidate_release_execution_job_commands"
    __table_args__ = (
        UniqueConstraint("authorization_id", name="uq_release_job_command_authorization"),
        CheckConstraint("verification_version BETWEEN 1 AND 8", name="ck_release_command_version"),
        CheckConstraint(
            "(outcome='applied' AND http_status=201 AND execution_job_id IS NOT NULL AND "
            "error_code IS NULL AND error_message IS NULL) OR (outcome='replayed' AND "
            "http_status=200 AND execution_job_id IS NOT NULL AND error_code IS NULL AND "
            "error_message IS NULL) OR (outcome='error' AND http_status IN (404,409) AND "
            "execution_job_id IS NULL AND error_code IS NOT NULL AND error_message IS NOT "
            "NULL)",
            name="ck_release_command_outcome",
        ),
        CheckConstraint(
            "octet_length(raw_body) BETWEEN 2 AND 65536 AND jsonb_typeof(command)='object' "
            "AND octet_length(command::text)<=32768 AND command_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_release_command_body",
        ),
        Index(
            "ix_release_job_commands_cursor",
            "investigation_run_id",
            "candidate_job_id",
            "verification_version",
            "cursor",
        ),
    )
    cursor: Mapped[int] = mapped_column(Integer, Identity(), primary_key=True)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_job_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    verification_version: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False)
    command: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_job_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("candidate_release_execution_jobs.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
