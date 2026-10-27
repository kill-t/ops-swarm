"""回流准入请求与逐命令审计的不可变 PostgreSQL 映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Identity,
    Index,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class CandidateReinvestigationRow(Base):
    __tablename__ = "candidate_reinvestigation_admissions"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(content)='object' AND octet_length(content::text)<=524288 "
            "AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_reinvestigation_admission_content",
        ),
    )

    id: Mapped[UUID] = mapped_column(primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidate_incident_convergences.investigation_run_id", ondelete="RESTRICT"),
        unique=True,
        nullable=False,
    )
    incident_id: Mapped[UUID] = mapped_column(
        ForeignKey("incidents.id", ondelete="RESTRICT"), nullable=False
    )
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    content: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class CandidateReinvestigationCommandRow(Base):
    __tablename__ = "candidate_reinvestigation_commands"
    __table_args__ = (
        CheckConstraint(
            "jsonb_typeof(command)='object' AND octet_length(command::text)<=32768 "
            "AND octet_length(raw_body) BETWEEN 2 AND 65536 "
            "AND command_sha256 ~ '^[0-9a-f]{64}$' "
            "AND char_length(btrim(subject_id)) BETWEEN 1 AND 100",
            name="ck_reinvestigation_command_content",
        ),
        CheckConstraint(
            "(outcome IN ('applied','replayed') AND request_id IS NOT NULL "
            "AND error_code IS NULL AND error_message IS NULL AND "
            "((outcome='applied' AND http_status=201) OR "
            "(outcome='replayed' AND http_status=200))) OR "
            "(outcome='error' AND request_id IS NULL AND http_status IN (404,409) "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL)",
            name="ck_reinvestigation_command_outcome",
        ),
        Index("ix_reinvestigation_commands_run_cursor", "investigation_run_id", "cursor"),
    )

    cursor: Mapped[int] = mapped_column(BigInteger, Identity(always=True), primary_key=True)
    authorization_id: Mapped[UUID] = mapped_column(
        ForeignKey("operator_authorizations.id", ondelete="RESTRICT"), unique=True, nullable=False
    )
    investigation_run_id: Mapped[UUID] = mapped_column(nullable=False)
    subject_id: Mapped[str] = mapped_column(String(100), nullable=False)
    command: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    raw_body: Mapped[str] = mapped_column(Text, nullable=False)
    command_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    http_status: Mapped[int] = mapped_column(nullable=False)
    request_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("candidate_reinvestigation_admissions.id", ondelete="RESTRICT")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
