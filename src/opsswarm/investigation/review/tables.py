"""开放假设独立审查的不可变 PostgreSQL 映射。"""

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


class InvestigationHypothesisReviewRow(Base):
    __tablename__ = "investigation_hypothesis_reviews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["open_hypothesis_id", "round_id", "investigation_run_id"],
            [
                "investigation_open_hypotheses.id",
                "investigation_open_hypotheses.round_id",
                "investigation_open_hypotheses.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_hypothesis_reviews_open_hypothesis",
        ),
        ForeignKeyConstraint(
            ["investigation_attempt_id", "investigation_run_id"],
            ["investigation_attempts.id", "investigation_attempts.investigation_run_id"],
            ondelete="RESTRICT",
            name="fk_hypothesis_reviews_parent_attempt",
        ),
        ForeignKeyConstraint(
            ["source_call_id", "investigation_run_id"],
            [
                "investigation_call_reservations.id",
                "investigation_call_reservations.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_hypothesis_reviews_source_call",
        ),
        UniqueConstraint(
            "open_hypothesis_id",
            "investigation_run_id",
            name="uq_hypothesis_reviews_open_hypothesis_run",
        ),
        UniqueConstraint(
            "source_call_id", "investigation_run_id", name="uq_hypothesis_reviews_source_run"
        ),
        UniqueConstraint(
            "id",
            "open_hypothesis_id",
            "round_id",
            "investigation_run_id",
            name="uq_hypothesis_reviews_route_source",
        ),
        CheckConstraint(
            "version = 'hypothesis-review-v1' AND status = 'recorded' AND "
            "COALESCE(review ->> 'version', '') = version AND "
            "COALESCE(review ->> 'status', '') = status",
            name="ck_hypothesis_reviews_contract",
        ),
        CheckConstraint(
            "jsonb_typeof(review) = 'object' AND octet_length(review::text) <= 65536",
            name="ck_hypothesis_reviews_size",
        ),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_hypothesis_reviews_hash"),
        Index("ix_hypothesis_reviews_run_created", "investigation_run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    round_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    open_hypothesis_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    investigation_attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_call_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    review: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
