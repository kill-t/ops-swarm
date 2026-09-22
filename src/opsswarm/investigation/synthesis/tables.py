"""动态开放假设的不可变 PostgreSQL 映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class InvestigationOpenHypothesisRow(Base):
    __tablename__ = "investigation_open_hypotheses"
    __table_args__ = (
        ForeignKeyConstraint(
            ["round_id", "investigation_run_id"],
            [
                "investigation_coordination_plans.round_id",
                "investigation_coordination_plans.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_open_hypotheses_round_plan",
        ),
        ForeignKeyConstraint(
            ["investigation_attempt_id", "investigation_run_id"],
            ["investigation_attempts.id", "investigation_attempts.investigation_run_id"],
            ondelete="RESTRICT",
            name="fk_open_hypotheses_parent_attempt",
        ),
        ForeignKeyConstraint(
            ["source_call_id", "investigation_run_id"],
            [
                "investigation_call_reservations.id",
                "investigation_call_reservations.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_open_hypotheses_source_call",
        ),
        UniqueConstraint("round_id", "investigation_run_id", name="uq_open_hypotheses_round_run"),
        UniqueConstraint(
            "id", "round_id", "investigation_run_id", name="uq_open_hypotheses_id_round_run"
        ),
        UniqueConstraint(
            "source_call_id", "investigation_run_id", name="uq_open_hypotheses_source_run"
        ),
        CheckConstraint(
            "version = 'open-hypothesis-v1' AND status = 'pending_review' AND "
            "COALESCE(hypothesis ->> 'version', '') = version AND "
            "COALESCE(hypothesis ->> 'status', '') = status",
            name="ck_open_hypotheses_contract",
        ),
        CheckConstraint(
            "jsonb_typeof(hypothesis) = 'object' AND octet_length(hypothesis::text) <= 65536",
            name="ck_open_hypotheses_size",
        ),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_open_hypotheses_hash"),
        Index("ix_open_hypotheses_run_created", "investigation_run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    round_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    investigation_attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_call_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    hypothesis: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
