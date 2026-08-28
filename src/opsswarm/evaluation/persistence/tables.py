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


class EvaluationExperimentRow(Base):
    """一组重复运行共享且不可静默改写的可比配置。"""

    __tablename__ = "evaluation_experiments"
    __table_args__ = (
        CheckConstraint(
            "evaluation_kind IN ('fixture_contract', 'real_model')",
            name="ck_evaluation_experiments_kind",
        ),
        CheckConstraint(
            "(evaluation_kind = 'fixture_contract' "
            "AND model_provider IS NULL AND model_name IS NULL "
            "AND model_parameters_hash IS NULL) OR "
            "(evaluation_kind = 'real_model' "
            "AND model_provider IS NOT NULL AND model_name IS NOT NULL "
            "AND model_parameters_hash ~ '^[0-9a-f]{64}$')",
            name="ck_evaluation_experiments_model_identity",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    experiment_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    evaluation_case_set_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("evaluation_cases.id", ondelete="RESTRICT"),
        nullable=False,
    )
    evaluation_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    runner_name: Mapped[str] = mapped_column(String(100), nullable=False)
    model_provider: Mapped[str | None] = mapped_column(String(100), nullable=True)
    model_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    model_parameters_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    single_agent_prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    multi_agent_prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
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
        CheckConstraint(
            "repetition_index BETWEEN 1 AND 1000",
            name="ck_evaluation_runs_repetition_index",
        ),
        CheckConstraint(
            "jsonb_typeof(single_agent_telemetry) = 'object'",
            name="ck_evaluation_runs_single_telemetry_object",
        ),
        CheckConstraint(
            "jsonb_typeof(multi_agent_telemetry) = 'object'",
            name="ck_evaluation_runs_multi_telemetry_object",
        ),
        UniqueConstraint(
            "experiment_id",
            "repetition_index",
            name="uq_evaluation_runs_experiment_repetition",
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
    experiment_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("evaluation_experiments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    repetition_index: Mapped[int] = mapped_column(Integer, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    report_content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    runner_name: Mapped[str] = mapped_column(String(100), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    single_agent_aggregate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    multi_agent_aggregate: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    aggregate_delta: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    single_agent_telemetry: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    multi_agent_telemetry: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class EvaluationAttemptRow(Base):
    """一个真实模型场景在一种 Agent 模式下的可审计生命周期。"""

    __tablename__ = "evaluation_attempts"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('single_agent', 'multi_agent')",
            name="ck_evaluation_attempts_mode",
        ),
        CheckConstraint(
            "status IN ('running', 'succeeded', 'failed')",
            name="ck_evaluation_attempts_status",
        ),
        CheckConstraint(
            "failure_category IS NULL OR failure_category IN "
            "('model_request_error', 'tool_error', 'timeout', "
            "'structured_output_error', 'safety_rejection', 'budget_exceeded')",
            name="ck_evaluation_attempts_failure_category",
        ),
        CheckConstraint(
            "repetition_index BETWEEN 1 AND 1000",
            name="ck_evaluation_attempts_repetition_index",
        ),
        CheckConstraint(
            "jsonb_typeof(budget_snapshot) = 'object'",
            name="ck_evaluation_attempts_budget_object",
        ),
        CheckConstraint(
            "jsonb_typeof(telemetry) = 'object'",
            name="ck_evaluation_attempts_telemetry_object",
        ),
        CheckConstraint(
            "result_snapshot IS NULL OR jsonb_typeof(result_snapshot) = 'object'",
            name="ck_evaluation_attempts_result_object",
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND result_snapshot IS NULL "
            "AND failure_category IS NULL AND failure_message IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND result_snapshot IS NOT NULL "
            "AND failure_category IS NULL AND failure_message IS NULL) OR "
            "(status = 'failed' AND finished_at IS NOT NULL AND result_snapshot IS NULL "
            "AND failure_category IS NOT NULL AND failure_message IS NOT NULL)",
            name="ck_evaluation_attempts_terminal_shape",
        ),
        CheckConstraint(
            "finished_at IS NULL OR finished_at >= started_at",
            name="ck_evaluation_attempts_time_order",
        ),
        UniqueConstraint(
            "experiment_id",
            "repetition_index",
            "scenario_id",
            "mode",
            name="uq_evaluation_attempts_experiment_slot",
        ),
        Index("ix_evaluation_attempts_sequence", "sequence"),
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
    experiment_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("evaluation_experiments.id", ondelete="RESTRICT"),
        nullable=False,
    )
    repetition_index: Mapped[int] = mapped_column(Integer, nullable=False)
    scenario_id: Mapped[str] = mapped_column(String(100), nullable=False)
    mode: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    budget_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    telemetry: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    result_snapshot: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB(none_as_null=True),
        nullable=True,
    )
    failure_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    failure_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
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
