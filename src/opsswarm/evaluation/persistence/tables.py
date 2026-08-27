"""版本化评测集、评测运行与逐场景结果的 SQLAlchemy 映射。"""

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
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class EvaluationCaseSetRow(Base):
    """一份不可静默改写的版本化评测集快照。"""

    __tablename__ = "evaluation_cases"
    __table_args__ = (
        CheckConstraint(
            "dataset_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_evaluation_cases_content_hash",
        ),
        CheckConstraint(
            "scenario_count BETWEEN 1 AND 100",
            name="ck_evaluation_cases_scenario_count",
        ),
        CheckConstraint(
            "jsonb_typeof(dataset_snapshot) = 'object'",
            name="ck_evaluation_cases_snapshot_object",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    dataset_key: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    dataset_content_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    scoring_policy_version: Mapped[str] = mapped_column(String(100), nullable=False)
    scenario_count: Mapped[int] = mapped_column(Integer, nullable=False)
    dataset_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EvaluationRunRow(Base):
    """一次已完成的公平对照运行及其聚合指标。"""

    __tablename__ = "evaluation_runs"
    __table_args__ = (
        CheckConstraint(
            "report_content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_evaluation_runs_content_hash",
        ),
        CheckConstraint(
            "jsonb_typeof(single_agent_aggregate) = 'object'",
            name="ck_evaluation_runs_single_aggregate_object",
        ),
        CheckConstraint(
            "jsonb_typeof(multi_agent_aggregate) = 'object'",
            name="ck_evaluation_runs_multi_aggregate_object",
        ),
        CheckConstraint(
            "jsonb_typeof(aggregate_delta) = 'object'",
            name="ck_evaluation_runs_delta_object",
        ),
        CheckConstraint(
            "generated_at <= created_at",
            name="ck_evaluation_runs_time_order",
        ),
        Index("ix_evaluation_runs_sequence", "sequence"),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    sequence: Mapped[int] = mapped_column(
        BigInteger,
        Identity(always=True),
        nullable=False,
        unique=True,
    )
    evaluation_case_set_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("evaluation_cases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    report_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_name: Mapped[str] = mapped_column(String(100), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    single_agent_aggregate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    multi_agent_aggregate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    aggregate_delta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EvaluationResultRow(Base):
    """一次评测运行中的单个场景对照结果。"""

    __tablename__ = "evaluation_results"
    __table_args__ = (
        CheckConstraint("position >= 1", name="ck_evaluation_results_position"),
        CheckConstraint(
            "jsonb_typeof(comparison) = 'object'",
            name="ck_evaluation_results_comparison_object",
        ),
        UniqueConstraint(
            "evaluation_run_id",
            "scenario_id",
            name="uq_evaluation_results_run_scenario",
        ),
        UniqueConstraint(
            "evaluation_run_id",
            "position",
            name="uq_evaluation_results_run_position",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    evaluation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("evaluation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    scenario_id: Mapped[str] = mapped_column(String(100), nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    comparison: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
