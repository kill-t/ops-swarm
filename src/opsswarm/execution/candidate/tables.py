"""候选只读执行前检查及可信命令审计的持久映射。"""

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


class CandidatePreflightRow(Base):
    __tablename__ = "candidate_execution_preflights"
    __table_args__ = (
        ForeignKeyConstraint(
            ["candidate_job_id", "investigation_run_id"],
            [
                "investigation_candidate_evaluation_jobs.id",
                "investigation_candidate_evaluation_jobs.investigation_run_id",
            ],
            name="fk_candidate_preflight_job_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("cursor", name="uq_candidate_preflight_cursor"),
        UniqueConstraint("authorization_id", name="uq_candidate_preflight_authorization"),
        UniqueConstraint(
            "investigation_run_id",
            "requested_by_id",
            "idempotency_key",
            name="uq_candidate_preflight_key",
        ),
        CheckConstraint(
            "policy_version='candidate-execution-preflight-v1'",
            name="ck_candidate_preflight_policy",
        ),
        CheckConstraint("incident_version>=2", name="ck_candidate_preflight_version"),
        CheckConstraint(
            "decision_content_sha256 ~ '^[0-9a-f]{64}$' AND "
            "command_sha256 ~ '^[0-9a-f]{64}$' AND "
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_candidate_preflight_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=65536",
            name="ck_candidate_preflight_content",
        ),
        CheckConstraint(
            "char_length(btrim(requested_by_id)) BETWEEN 1 AND 100",
            name="ck_candidate_preflight_subject",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 AND "
            "idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_candidate_preflight_key",
        ),
        CheckConstraint(
            "expires_at>created_at AND expires_at<=created_at+interval '30 seconds'",
            name="ck_candidate_preflight_expiry",
        ),
        Index(
            "ix_candidate_preflight_run_job_cursor",
            "investigation_run_id",
            "candidate_job_id",
            "cursor",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    cursor: Mapped[int] = mapped_column(BigInteger, Identity(always=True), nullable=False)
    operation_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_job_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    decision_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_approval_decisions.id", ondelete="RESTRICT"), nullable=False
    )
    decision_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    incident_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    requested_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CandidatePreflightCommandRow(Base):
    __tablename__ = "candidate_preflight_commands"
    __table_args__ = (
        UniqueConstraint("authorization_id", name="uq_candidate_preflight_commands_authorization"),
        CheckConstraint(
            "jsonb_typeof(command)='object' AND octet_length(command::text)<=32768",
            name="ck_candidate_preflight_commands_body",
        ),
        CheckConstraint(
            "octet_length(raw_body) BETWEEN 2 AND 65536", name="ck_candidate_preflight_commands_raw"
        ),
        CheckConstraint(
            "command_sha256 ~ '^[0-9a-f]{64}$'", name="ck_candidate_preflight_commands_hash"
        ),
        CheckConstraint(
            "char_length(btrim(subject_id)) BETWEEN 1 AND 100",
            name="ck_candidate_preflight_commands_subject",
        ),
        CheckConstraint(
            "(outcome IN ('applied','replayed') AND preflight_id IS NOT NULL "
            "AND error_code IS NULL "
            "AND error_message IS NULL AND ((outcome='applied' AND http_status=201) OR "
            "(outcome='replayed' AND http_status=200))) OR "
            "(outcome='error' AND preflight_id IS NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL "
            "AND http_status IN (404,409))",
            name="ck_candidate_preflight_commands_outcome",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$'",
            name="ck_candidate_preflight_commands_error",
        ),
        Index("ix_candidate_preflight_commands_run_cursor", "investigation_run_id", "cursor"),
    )

    cursor: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_job_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False)
    command: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    preflight_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("candidate_execution_preflights.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
