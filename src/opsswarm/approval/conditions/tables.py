"""精确核验版本的独立审批事实与两阶段命令审计映射。"""

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


def _fact_constraints(prefix: str, subject: str) -> tuple[Any, ...]:
    return (
        ForeignKeyConstraint(
            ["candidate_job_id", "investigation_run_id"],
            [
                "investigation_candidate_evaluation_jobs.id",
                "investigation_candidate_evaluation_jobs.investigation_run_id",
            ],
            name=f"fk_{prefix}_job_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("authorization_id", name=f"uq_{prefix}_authorization"),
        UniqueConstraint(
            "investigation_run_id", subject, "idempotency_key", name=f"uq_{prefix}_key"
        ),
        CheckConstraint("policy_version='condition-approval-v1'", name=f"ck_{prefix}_policy"),
        CheckConstraint(
            "incident_version>=2 AND verification_version BETWEEN 1 AND 8",
            name=f"ck_{prefix}_version",
        ),
        CheckConstraint(
            "verification_content_sha256 ~ '^[0-9a-f]{64}$' AND "
            "command_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name=f"ck_{prefix}_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=65536",
            name=f"ck_{prefix}_content",
        ),
        CheckConstraint(
            f"char_length(btrim({subject})) BETWEEN 1 AND 100", name=f"ck_{prefix}_subject"
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 AND "
            "idempotency_key ~ '^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name=f"ck_{prefix}_key",
        ),
    )


class ConditionApprovalRequestRow(Base):
    __tablename__ = "condition_approval_requests"
    __table_args__ = (
        *_fact_constraints("condition_approval_request", "requested_by_id"),
        UniqueConstraint("verification_id", name="uq_condition_approval_request_verification"),
        CheckConstraint("status='pending'", name="ck_condition_approval_request_status"),
        CheckConstraint("expires_at>created_at", name="ck_condition_approval_request_expiry"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_job_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    verification_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_condition_verifications.id", ondelete="RESTRICT"), nullable=False
    )
    verification_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_version: Mapped[int] = mapped_column(Integer, nullable=False)
    incident_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
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


class ConditionApprovalDecisionRow(Base):
    __tablename__ = "condition_approval_decisions"
    __table_args__ = (
        *_fact_constraints("condition_approval_decision", "decided_by_id"),
        UniqueConstraint("request_id", name="uq_condition_approval_decision_request"),
        CheckConstraint(
            "request_content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_condition_approval_decision_request",
        ),
        CheckConstraint(
            "outcome IN ('approved','rejected','expired')",
            name="ck_condition_approval_decision_outcome",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_job_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("condition_approval_requests.id", ondelete="RESTRICT"), nullable=False
    )
    request_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_condition_verifications.id", ondelete="RESTRICT"), nullable=False
    )
    verification_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    verification_version: Mapped[int] = mapped_column(Integer, nullable=False)
    incident_version: Mapped[int] = mapped_column(Integer, nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    decided_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ConditionApprovalCommandRow(Base):
    __tablename__ = "condition_approval_commands"
    __table_args__ = (
        UniqueConstraint("authorization_id", name="uq_condition_approval_commands_authorization"),
        CheckConstraint(
            "jsonb_typeof(command)='object' AND "
            "octet_length(convert_to(candidate_canonical_json(command),'UTF8'))<=32768",
            name="ck_condition_approval_commands_body",
        ),
        CheckConstraint(
            "octet_length(raw_body) BETWEEN 2 AND 65536", name="ck_condition_approval_commands_raw"
        ),
        CheckConstraint(
            "stage IN ('request','decision')", name="ck_condition_approval_commands_stage"
        ),
        CheckConstraint(
            "verification_version BETWEEN 1 AND 8", name="ck_condition_approval_commands_version"
        ),
        CheckConstraint(
            "command_sha256 ~ '^[0-9a-f]{64}$'", name="ck_condition_approval_commands_hash"
        ),
        CheckConstraint(
            "char_length(btrim(subject_id)) BETWEEN 1 AND 100",
            name="ck_condition_approval_commands_subject",
        ),
        CheckConstraint(
            "(outcome IN ('applied','replayed') AND request_id IS NOT NULL AND "
            "((stage='request' AND decision_id IS NULL) OR "
            "(stage='decision' AND decision_id IS NOT NULL)) AND error_code IS NULL "
            "AND error_message IS NULL AND ((outcome='applied' AND http_status=201) OR "
            "(outcome='replayed' AND http_status=200))) OR "
            "(outcome='error' AND request_id IS NULL AND decision_id IS NULL AND "
            "error_code IS NOT NULL AND error_message IS NOT NULL AND http_status IN (404,409))",
            name="ck_condition_approval_commands_outcome",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$'",
            name="ck_condition_approval_commands_error",
        ),
        Index(
            "ix_condition_approval_commands_run_job_version_cursor",
            "investigation_run_id",
            "candidate_job_id",
            "verification_version",
            "cursor",
        ),
    )

    cursor: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_job_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    verification_version: Mapped[int] = mapped_column(Integer, nullable=False)
    stage: Mapped[str] = mapped_column(String(16), nullable=False)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False)
    command: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("condition_approval_requests.id", ondelete="RESTRICT")
    )
    decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("condition_approval_decisions.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
