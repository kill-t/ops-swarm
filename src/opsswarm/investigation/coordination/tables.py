"""运行级协调策略与轮次计划的不可变 PostgreSQL 映射。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class InvestigationCoordinationPolicyRow(Base):
    __tablename__ = "investigation_coordination_policies"
    __table_args__ = (
        UniqueConstraint(
            "investigation_run_id",
            "content_sha256",
            name="uq_coordination_policies_run_hash",
        ),
        CheckConstraint(
            "version = 'dynamic-coordination-v1' AND COALESCE(policy ->> 'version', '') = version",
            name="ck_coordination_policies_version",
        ),
        CheckConstraint(
            "jsonb_typeof(policy) = 'object' AND octet_length(policy::text) <= 8192",
            name="ck_coordination_policies_size",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_coordination_policies_hash",
        ),
    )

    investigation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    policy: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class InvestigationCoordinationPlanRow(Base):
    __tablename__ = "investigation_coordination_plans"
    __table_args__ = (
        UniqueConstraint(
            "round_id", "investigation_run_id", name="uq_coordination_plans_round_run"
        ),
        ForeignKeyConstraint(
            ["round_id", "investigation_run_id"],
            ["investigation_rounds.id", "investigation_rounds.investigation_run_id"],
            ondelete="RESTRICT",
            name="fk_coordination_plans_round_run",
        ),
        ForeignKeyConstraint(
            ["investigation_run_id", "policy_sha256"],
            [
                "investigation_coordination_policies.investigation_run_id",
                "investigation_coordination_policies.content_sha256",
            ],
            ondelete="RESTRICT",
            name="fk_coordination_plans_policy",
        ),
        ForeignKeyConstraint(
            ["investigation_attempt_id", "investigation_run_id"],
            ["investigation_attempts.id", "investigation_attempts.investigation_run_id"],
            ondelete="RESTRICT",
            name="fk_coordination_plans_parent_attempt",
        ),
        ForeignKeyConstraint(
            ["source_call_id", "investigation_run_id"],
            [
                "investigation_call_reservations.id",
                "investigation_call_reservations.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_coordination_plans_source_call",
        ),
        ForeignKeyConstraint(
            ["source_review_route_id", "investigation_run_id"],
            [
                "investigation_hypothesis_review_routes.id",
                "investigation_hypothesis_review_routes.investigation_run_id",
            ],
            ondelete="RESTRICT",
            use_alter=True,
            name="fk_coordination_plans_source_review_route",
        ),
        UniqueConstraint(
            "source_review_route_id",
            "investigation_run_id",
            name="uq_coordination_plans_source_review_route",
        ),
        CheckConstraint(
            "source_call_id IS NULL OR source_review_route_id IS NULL",
            name="ck_coordination_plans_source_exclusive",
        ),
        CheckConstraint(
            "version = 'compiled-coordination-plan-v1' AND "
            "COALESCE(plan ->> 'version', '') = version AND "
            "COALESCE(plan ->> 'policy_version', '') = 'dynamic-coordination-v1' AND "
            "COALESCE(plan #>> '{proposal,version}', '') = 'coordination-proposal-v1'",
            name="ck_coordination_plans_version",
        ),
        CheckConstraint(
            "jsonb_typeof(plan) = 'object' AND octet_length(plan::text) <= 65536",
            name="ck_coordination_plans_size",
        ),
        CheckConstraint(
            "policy_sha256 ~ '^[0-9a-f]{64}$' AND content_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_coordination_plans_hash",
        ),
        Index("ix_coordination_plans_run_created", "investigation_run_id", "created_at"),
    )

    round_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    investigation_attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    source_call_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    source_review_route_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    version: Mapped[str] = mapped_column(String(40), nullable=False)
    policy_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
