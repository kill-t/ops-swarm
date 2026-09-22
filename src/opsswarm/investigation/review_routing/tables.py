"""确定性审查路由事实的不可变 PostgreSQL 映射。"""

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


class InvestigationHypothesisReviewRouteRow(Base):
    __tablename__ = "investigation_hypothesis_review_routes"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "hypothesis_review_id",
                "open_hypothesis_id",
                "round_id",
                "investigation_run_id",
            ],
            [
                "investigation_hypothesis_reviews.id",
                "investigation_hypothesis_reviews.open_hypothesis_id",
                "investigation_hypothesis_reviews.round_id",
                "investigation_hypothesis_reviews.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_hypothesis_review_routes_review",
        ),
        ForeignKeyConstraint(
            ["investigation_attempt_id", "investigation_run_id"],
            ["investigation_attempts.id", "investigation_attempts.investigation_run_id"],
            ondelete="RESTRICT",
            name="fk_hypothesis_review_routes_parent",
        ),
        ForeignKeyConstraint(
            ["investigation_run_id", "policy_sha256"],
            [
                "investigation_coordination_policies.investigation_run_id",
                "investigation_coordination_policies.content_sha256",
            ],
            ondelete="RESTRICT",
            name="fk_hypothesis_review_routes_policy",
        ),
        UniqueConstraint(
            "hypothesis_review_id",
            "investigation_run_id",
            name="uq_hypothesis_review_routes_review_run",
        ),
        UniqueConstraint(
            "round_id",
            "investigation_run_id",
            name="uq_hypothesis_review_routes_round_run",
        ),
        CheckConstraint(
            "version = 'review-routing-v1' AND status = 'recorded' AND "
            "COALESCE(decision ->> 'version', '') = version AND "
            "COALESCE(decision ->> 'status', '') = status AND "
            "COALESCE(decision ->> 'rule_version', '') = "
            "'deterministic-review-routing-v1' AND "
            "COALESCE(decision ->> 'route', '') IN "
            "('follow_up', 'await_human', 'candidate_evaluation')",
            name="ck_hypothesis_review_routes_contract",
        ),
        CheckConstraint(
            "jsonb_typeof(decision) = 'object' AND octet_length(decision::text) <= 32768",
            name="ck_hypothesis_review_routes_size",
        ),
        CheckConstraint(
            "review_sha256 ~ '^[0-9a-f]{64}$' AND "
            "policy_sha256 ~ '^[0-9a-f]{64}$' AND "
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_hypothesis_review_routes_hash",
        ),
        Index("ix_hypothesis_review_routes_run_created", "investigation_run_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    round_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    open_hypothesis_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    hypothesis_review_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    investigation_attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    decision: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    review_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
