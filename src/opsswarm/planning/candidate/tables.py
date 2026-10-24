"""独立候选草案与可信命令审计，不加入历史审批执行表。"""

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


class CandidatePlanRow(Base):
    __tablename__ = "candidate_action_plans"
    __table_args__ = (
        ForeignKeyConstraint(
            ["candidate_job_id", "investigation_run_id"],
            [
                "investigation_candidate_evaluation_jobs.id",
                "investigation_candidate_evaluation_jobs.investigation_run_id",
            ],
            name="fk_candidate_plans_job_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("candidate_job_id", name="uq_candidate_plans_job"),
        UniqueConstraint("id", "investigation_run_id", name="uq_candidate_plans_id_run"),
        UniqueConstraint("authorization_id", name="uq_candidate_plans_authorization"),
        UniqueConstraint(
            "investigation_run_id",
            "requested_by_id",
            "idempotency_key",
            name="uq_candidate_plans_key",
        ),
        CheckConstraint("policy_version='candidate-plan-v1'", name="ck_candidate_plans_policy"),
        CheckConstraint(
            "wait_version IS NULL OR wait_version BETWEEN 2 AND 16",
            name="ck_candidate_plans_wait_version",
        ),
        CheckConstraint("incident_version>=1", name="ck_candidate_plans_incident_version"),
        CheckConstraint(
            "candidate_result_sha256 ~ '^[0-9a-f]{64}$' AND "
            "command_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_candidate_plans_hashes",
        ),
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=65536",
            name="ck_candidate_plans_content",
        ),
        CheckConstraint(
            "char_length(btrim(requested_by_id)) BETWEEN 1 AND 100",
            name="ck_candidate_plans_subject",
        ),
        CheckConstraint(
            "char_length(idempotency_key) BETWEEN 8 AND 100 AND idempotency_key ~ "
            "'^[A-Za-z0-9][A-Za-z0-9._:-]+$'",
            name="ck_candidate_plans_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_job_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    candidate_result_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    wait_version: Mapped[int | None] = mapped_column(Integer)
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


class CandidatePlanCommandRow(Base):
    __tablename__ = "candidate_plan_commands"
    __table_args__ = (
        UniqueConstraint("authorization_id", name="uq_candidate_plan_commands_authorization"),
        CheckConstraint(
            "jsonb_typeof(command)='object' AND octet_length(command::text)<=32768",
            name="ck_candidate_plan_commands_body",
        ),
        CheckConstraint(
            "octet_length(raw_body) BETWEEN 2 AND 65536", name="ck_candidate_plan_commands_raw"
        ),
        CheckConstraint(
            "command_sha256 ~ '^[0-9a-f]{64}$'", name="ck_candidate_plan_commands_hash"
        ),
        CheckConstraint(
            "char_length(btrim(subject_id)) BETWEEN 1 AND 100",
            name="ck_candidate_plan_commands_subject",
        ),
        CheckConstraint(
            "(outcome IN ('applied','replayed') AND plan_id IS NOT NULL AND error_code IS NULL "
            "AND error_message IS NULL AND ((outcome='applied' AND http_status=201) OR "
            "(outcome='replayed' AND http_status=200))) OR (outcome='error' AND plan_id IS NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL AND http_status IN "
            "(404,409))",
            name="ck_candidate_plan_commands_outcome",
        ),
        CheckConstraint(
            "error_code IS NULL OR error_code ~ '^[a-z0-9_]{1,100}$'",
            name="ck_candidate_plan_commands_error",
        ),
        Index("ix_candidate_plan_commands_run_cursor", "investigation_run_id", "cursor"),
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
    plan_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("candidate_action_plans.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
