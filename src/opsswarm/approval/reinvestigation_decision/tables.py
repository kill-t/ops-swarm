"""独立候选审批决定及可信命令审计的持久映射。"""

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


class ReflowApprovalDecisionRow(Base):
    __tablename__ = "reinvestigation_approval_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["candidate_job_id", "investigation_run_id"],
            [
                "investigation_reflow_candidate_jobs.id",
                "investigation_reflow_candidate_jobs.investigation_run_id",
            ],
            name="fk_reflow_decisions_job_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("request_id", name="uq_reflow_decisions_request"),
        UniqueConstraint("authorization_id", name="uq_reflow_decisions_authorization"),
        UniqueConstraint("event_cursor", name="uq_reflow_decisions_event"),
        UniqueConstraint(
            "investigation_run_id",
            "decided_by_id",
            "idempotency_key",
            name="uq_reflow_decisions_key",
        ),
        CheckConstraint(
            "candidate_job_id=investigation_run_id", name="ck_reflow_decisions_identity"
        ),
        CheckConstraint(
            "basis_event_cursor>=1 AND event_cursor>basis_event_cursor",
            name="ck_reflow_decisions_basis",
        ),
        CheckConstraint(
            "isfinite(credential_expires_at) AND created_at<credential_expires_at",
            name="ck_reflow_decisions_credential",
        ),
        CheckConstraint(
            "policy_version='reinvestigation-approval-decision-v1'",
            name="ck_reflow_decisions_policy",
        ),
        CheckConstraint(
            "outcome IN ('approved','rejected','expired')", name="ck_reflow_decisions_outcome"
        ),
        CheckConstraint(
            "previous_incident_version>=1 AND incident_version=previous_incident_version+1",
            name="ck_reflow_decisions_versions",
        ),
        CheckConstraint(
            "plan_content_sha256 ~ '^[0-9a-f]{64}$' AND command_sha256 ~ '^[0-9a-f]{64}$' AND "
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reflow_decisions_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=131072",
            name="ck_reflow_decisions_content",
        ),
        CheckConstraint(
            "char_length(btrim(decided_by_id)) BETWEEN 1 AND 100",
            name="ck_reflow_decisions_subject",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 AND idempotency_key ~ "
            "'^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_reflow_decisions_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_job_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    request_id: Mapped[UUID] = mapped_column(
        ForeignKey("reinvestigation_approval_requests.id", ondelete="RESTRICT"), nullable=False
    )
    plan_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    basis_event_cursor: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incident_events.id", ondelete="RESTRICT"), nullable=False
    )
    previous_incident_version: Mapped[int] = mapped_column(Integer, nullable=False)
    incident_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_cursor: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incident_events.id", ondelete="RESTRICT"), nullable=False
    )
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    decided_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReflowApprovalDecisionCommandRow(Base):
    __tablename__ = "reinvestigation_approval_decision_commands"
    __table_args__ = (
        UniqueConstraint("authorization_id", name="uq_reflow_decision_commands_authorization"),
        CheckConstraint(
            "candidate_job_id=investigation_run_id",
            name="ck_reflow_decision_commands_identity",
        ),
        CheckConstraint(
            "isfinite(credential_expires_at) AND created_at<credential_expires_at",
            name="ck_reflow_decision_commands_credential",
        ),
        CheckConstraint(
            "jsonb_typeof(command)='object' AND octet_length(command::text)<=32768",
            name="ck_reflow_decision_commands_body",
        ),
        CheckConstraint(
            "octet_length(raw_body) BETWEEN 2 AND 65536",
            name="ck_reflow_decision_commands_raw",
        ),
        CheckConstraint(
            "command_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reflow_decision_commands_hash",
        ),
        CheckConstraint(
            "char_length(btrim(subject_id)) BETWEEN 1 AND 100",
            name="ck_reflow_decision_commands_subject",
        ),
        CheckConstraint(
            "(outcome IN ('applied','replayed') AND decision_id IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL AND ((outcome='applied' AND http_status=201) OR "
            "(outcome='replayed' AND http_status=200))) OR "
            "(outcome='error' AND decision_id IS NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL "
            "AND http_status IN (404,409))",
            name="ck_reflow_decision_commands_outcome",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$'",
            name="ck_reflow_decision_commands_error",
        ),
        Index(
            "ix_reflow_decision_commands_run_cursor",
            "investigation_run_id",
            "cursor",
        ),
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
    decision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("reinvestigation_approval_decisions.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    credential_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
