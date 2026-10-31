"""独立不可变候选审批请求与可信命令审计的持久映射。"""

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


class ReflowApprovalRequestRow(Base):
    __tablename__ = "reinvestigation_approval_requests"
    __table_args__ = (
        ForeignKeyConstraint(
            ["plan_id", "investigation_run_id"],
            [
                "reinvestigation_candidate_plans.id",
                "reinvestigation_candidate_plans.investigation_run_id",
            ],
            name="fk_reflow_approval_plan_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["candidate_job_id", "investigation_run_id"],
            [
                "investigation_reflow_candidate_jobs.id",
                "investigation_reflow_candidate_jobs.investigation_run_id",
            ],
            name="fk_reflow_approval_job_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("plan_id", name="uq_reflow_approval_plan"),
        UniqueConstraint("authorization_id", name="uq_reflow_approval_authorization"),
        UniqueConstraint("event_cursor", name="uq_reflow_approval_event"),
        UniqueConstraint(
            "investigation_run_id",
            "requested_by_id",
            "idempotency_key",
            name="uq_reflow_approval_key",
        ),
        CheckConstraint(
            "policy_version='reinvestigation-approval-v1'", name="ck_reflow_approval_policy"
        ),
        CheckConstraint("status='pending'", name="ck_reflow_approval_status"),
        CheckConstraint(
            "candidate_job_id=investigation_run_id", name="ck_reflow_approval_identity"
        ),
        CheckConstraint(
            "basis_event_cursor>=1 AND event_cursor>basis_event_cursor",
            name="ck_reflow_approval_basis",
        ),
        CheckConstraint(
            "isfinite(credential_expires_at) AND created_at<credential_expires_at",
            name="ck_reflow_approval_credential",
        ),
        CheckConstraint(
            "previous_incident_version>=1 AND incident_version=previous_incident_version+1",
            name="ck_reflow_approval_versions",
        ),
        CheckConstraint(
            "plan_content_sha256 ~ '^[0-9a-f]{64}$' AND command_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reflow_approval_hashes",
        ),
        CheckConstraint(
            "char_length(btrim(requested_by_id)) BETWEEN 1 AND 100",
            name="ck_reflow_approval_subject",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 AND idempotency_key ~ "
            "'^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_reflow_approval_key",
        ),
        CheckConstraint(
            "expires_at>created_at AND expires_at<=created_at+interval '30 minutes'",
            name="ck_reflow_approval_expiry",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_job_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    plan_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    plan_content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    basis_event_cursor: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incident_events.id", ondelete="RESTRICT"), nullable=False
    )
    previous_incident_version: Mapped[int] = mapped_column(Integer, nullable=False)
    incident_version: Mapped[int] = mapped_column(Integer, nullable=False)
    event_cursor: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("incident_events.id", ondelete="RESTRICT"), nullable=False
    )
    policy_version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_by_id: Mapped[str] = mapped_column(String(100), nullable=False)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), nullable=False
    )
    idempotency_key: Mapped[str] = mapped_column(String(100), nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ReflowApprovalCommandRow(Base):
    __tablename__ = "reinvestigation_approval_commands"
    __table_args__ = (
        CheckConstraint(
            "candidate_job_id=investigation_run_id", name="ck_reflow_approval_commands_identity"
        ),
        CheckConstraint(
            "isfinite(credential_expires_at) AND created_at<credential_expires_at",
            name="ck_reflow_approval_commands_credential",
        ),
        UniqueConstraint("authorization_id", name="uq_reflow_approval_commands_authorization"),
        CheckConstraint(
            "jsonb_typeof(command)='object' AND octet_length(command::text)<=32768",
            name="ck_reflow_approval_commands_body",
        ),
        CheckConstraint(
            "octet_length(raw_body) BETWEEN 2 AND 65536", name="ck_reflow_approval_commands_raw"
        ),
        CheckConstraint(
            "command_sha256 ~ '^[0-9a-f]{64}$'", name="ck_reflow_approval_commands_hash"
        ),
        CheckConstraint(
            "char_length(btrim(subject_id)) BETWEEN 1 AND 100",
            name="ck_reflow_approval_commands_subject",
        ),
        CheckConstraint(
            "(outcome IN ('applied','replayed') AND request_id IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL AND ((outcome='applied' AND http_status=201) OR "
            "(outcome='replayed' AND http_status=200))) OR (outcome='error' AND request_id IS NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL "
            "AND http_status IN (404,409))",
            name="ck_reflow_approval_commands_outcome",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$'",
            name="ck_reflow_approval_commands_error",
        ),
        Index("ix_reflow_approval_commands_run_cursor", "investigation_run_id", "cursor"),
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
    request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("reinvestigation_approval_requests.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    credential_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
