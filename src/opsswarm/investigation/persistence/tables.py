"""调查运行与 PostgreSQL 租约任务映射。"""

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base

RUN_STATUS_SQL = "'queued', 'running', 'succeeded', 'failed'"
WORK_STATUS_SQL = "'pending', 'running', 'completed', 'failed'"
ATTEMPT_STATUS_SQL = "'running', 'succeeded', 'failed', 'lease_expired'"


class InvestigationRunRow(Base):
    """一次调查的业务状态与最终结构化结果。"""

    __tablename__ = "investigation_runs"
    __table_args__ = (
        CheckConstraint(
            "mode IN ('single_agent', 'multi_agent')",
            name="ck_investigation_runs_mode",
        ),
        CheckConstraint(
            "data_source_mode IN ('frozen', 'live')",
            name="ck_investigation_runs_data_source_mode",
        ),
        CheckConstraint(
            "mode <> 'multi_agent' OR graph_thread_id IS NOT NULL",
            name="ck_investigation_runs_multi_thread",
        ),
        CheckConstraint(f"status IN ({RUN_STATUS_SQL})", name="ck_investigation_runs_status"),
        CheckConstraint(
            "result IS NULL OR jsonb_typeof(result) = 'object'",
            name="ck_investigation_runs_result_object",
        ),
        CheckConstraint(
            "metrics IS NULL OR jsonb_typeof(metrics) = 'object'",
            name="ck_investigation_runs_metrics_object",
        ),
        CheckConstraint(
            "last_error IS NULL OR jsonb_typeof(last_error) = 'object'",
            name="ck_investigation_runs_error_object",
        ),
        CheckConstraint(
            "(status = 'succeeded' AND result IS NOT NULL) OR "
            "(status <> 'succeeded' AND result IS NULL)",
            name="ck_investigation_runs_result_shape",
        ),
        CheckConstraint(
            "metrics IS NULL OR status = 'succeeded'",
            name="ck_investigation_runs_metrics_shape",
        ),
        CheckConstraint(
            "status <> 'succeeded' OR "
            "(data_source_mode = 'frozen' AND metrics IS NOT NULL) OR "
            "(data_source_mode = 'live' AND metrics IS NULL)",
            name="ck_investigation_runs_metric_scope",
        ),
        CheckConstraint(
            "status <> 'failed' OR last_error IS NOT NULL",
            name="ck_investigation_runs_failure_shape",
        ),
        CheckConstraint(
            "(status IN ('queued', 'running') AND completed_at IS NULL) OR "
            "(status IN ('succeeded', 'failed') AND completed_at IS NOT NULL)",
            name="ck_investigation_runs_completion_shape",
        ),
        CheckConstraint(
            "status = 'queued' OR started_at IS NOT NULL",
            name="ck_investigation_runs_started_shape",
        ),
        CheckConstraint("created_at <= updated_at", name="ck_investigation_runs_time_order"),
        CheckConstraint(
            "completed_at IS NULL OR completed_at >= created_at",
            name="ck_investigation_runs_completed_time",
        ),
        Index(
            "uq_investigation_runs_active_incident",
            "incident_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
        Index("ix_investigation_runs_incident_created", "incident_id", text("created_at DESC")),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    incident_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("incidents.id", ondelete="RESTRICT"),
        nullable=False,
    )
    mode: Mapped[str] = mapped_column(String(32), nullable=False)
    data_source_mode: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(100), nullable=False)
    graph_thread_id: Mapped[str | None] = mapped_column(String(100), unique=True)
    model_name: Mapped[str | None] = mapped_column(String(200))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    metrics: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorkItemRow(Base):
    """由 Worker 使用租约领取的至少一次处理任务。"""

    __tablename__ = "work_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('run_single_agent_investigation', 'run_multi_agent_investigation')",
            name="ck_work_items_kind",
        ),
        CheckConstraint(f"status IN ({WORK_STATUS_SQL})", name="ck_work_items_status"),
        CheckConstraint(
            "attempt_count >= 0 AND max_attempts >= 1 AND attempt_count <= max_attempts",
            name="ck_work_items_attempts",
        ),
        CheckConstraint(
            "last_error IS NULL OR jsonb_typeof(last_error) = 'object'",
            name="ck_work_items_error_object",
        ),
        CheckConstraint(
            "trace_context IS NULL OR jsonb_typeof(trace_context) = 'object'",
            name="ck_work_items_trace_context_object",
        ),
        CheckConstraint(
            "(status = 'running' AND locked_by IS NOT NULL AND locked_until IS NOT NULL "
            "AND lease_token IS NOT NULL) OR "
            "(status <> 'running' AND locked_by IS NULL AND locked_until IS NULL "
            "AND lease_token IS NULL)",
            name="ck_work_items_lease_shape",
        ),
        CheckConstraint(
            "(status IN ('pending', 'running') AND completed_at IS NULL) OR "
            "(status IN ('completed', 'failed') AND completed_at IS NOT NULL)",
            name="ck_work_items_completion_shape",
        ),
        CheckConstraint(
            "status <> 'failed' OR last_error IS NOT NULL",
            name="ck_work_items_failure_shape",
        ),
        CheckConstraint("created_at <= updated_at", name="ck_work_items_time_order"),
        Index(
            "ix_work_items_pending_claim",
            "available_at",
            "created_at",
            postgresql_where=text("status = 'pending'"),
        ),
        Index(
            "ix_work_items_expired_lease",
            "locked_until",
            postgresql_where=text("status = 'running'"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
    )
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked_by: Mapped[str | None] = mapped_column(String(100))
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    trace_context: Mapped[dict[str, str] | None] = mapped_column(JSONB(none_as_null=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class InvestigationAttemptRow(Base):
    """每次租约领取对应的可结算尝试历史。"""

    __tablename__ = "investigation_attempts"
    __table_args__ = (
        CheckConstraint(
            f"status IN ({ATTEMPT_STATUS_SQL})",
            name="ck_investigation_attempts_status",
        ),
        CheckConstraint(
            "attempt_number >= 1",
            name="ck_investigation_attempts_number",
        ),
        CheckConstraint(
            "model_request_count >= 0 AND input_tokens >= 0 AND output_tokens >= 0 "
            "AND total_tokens = input_tokens + output_tokens",
            name="ck_investigation_attempts_usage",
        ),
        CheckConstraint(
            "usage_complete OR (NOT usage_complete AND estimated_cost_usd IS NULL)",
            name="ck_investigation_attempts_usage_complete",
        ),
        CheckConstraint(
            "(input_cost_per_million_usd IS NULL AND output_cost_per_million_usd IS NULL) OR "
            "(input_cost_per_million_usd IS NOT NULL "
            "AND output_cost_per_million_usd IS NOT NULL "
            "AND input_cost_per_million_usd >= 0 AND output_cost_per_million_usd >= 0)",
            name="ck_investigation_attempts_pricing_pair",
        ),
        CheckConstraint(
            "status IN ('running', 'lease_expired') OR "
            "(usage_complete AND input_cost_per_million_usd IS NOT NULL "
            "AND estimated_cost_usd >= 0) OR "
            "(input_cost_per_million_usd IS NULL AND estimated_cost_usd IS NULL) OR "
            "(NOT usage_complete AND estimated_cost_usd IS NULL)",
            name="ck_investigation_attempts_cost_shape",
        ),
        CheckConstraint(
            "status <> 'lease_expired' OR (NOT usage_complete AND estimated_cost_usd IS NULL)",
            name="ck_investigation_attempts_expired_usage",
        ),
        CheckConstraint(
            "(status = 'running' AND finished_at IS NULL AND duration_ms IS NULL "
            "AND will_retry IS NULL AND error_code IS NULL AND error_message IS NULL "
            "AND estimated_cost_usd IS NULL) OR "
            "(status = 'succeeded' AND finished_at IS NOT NULL AND duration_ms >= 0 "
            "AND will_retry = false AND error_code IS NULL AND error_message IS NULL) OR "
            "(status IN ('failed', 'lease_expired') AND finished_at IS NOT NULL "
            "AND duration_ms >= 0 AND will_retry IS NOT NULL "
            "AND error_code IS NOT NULL AND error_message IS NOT NULL)",
            name="ck_investigation_attempts_terminal_shape",
        ),
        CheckConstraint(
            "started_at <= lease_expires_at AND (finished_at IS NULL OR finished_at >= started_at)",
            name="ck_investigation_attempts_time_order",
        ),
        UniqueConstraint(
            "work_item_id",
            "attempt_number",
            name="uq_investigation_attempts_work_number",
        ),
        Index(
            "ix_investigation_attempts_run_number",
            "investigation_run_id",
            "attempt_number",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    work_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("work_items.id", ondelete="RESTRICT"),
        nullable=False,
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    lease_token: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        nullable=False,
        unique=True,
    )
    status: Mapped[str] = mapped_column(String(24), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(100), nullable=False)
    model_name: Mapped[str | None] = mapped_column(String(200))
    input_cost_per_million_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    output_cost_per_million_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    model_request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    usage_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    input_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    estimated_cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(14, 6))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(BigInteger)
    will_retry: Mapped[bool | None] = mapped_column(Boolean)
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(String(500))


class EvidenceItemRow(Base):
    """经过大小限制和结构化的只读调查证据。"""

    __tablename__ = "evidence_items"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('log', 'metric', 'change')",
            name="ck_evidence_items_kind",
        ),
        CheckConstraint(
            "jsonb_typeof(attributes) = 'object'",
            name="ck_evidence_items_attributes_object",
        ),
        CheckConstraint(
            "content_hash ~ '^[0-9a-f]{64}$'",
            name="ck_evidence_items_content_hash",
        ),
        UniqueConstraint(
            "investigation_run_id",
            "evidence_key",
            name="uq_evidence_items_run_key",
        ),
        UniqueConstraint(
            "investigation_run_id",
            "id",
            name="uq_evidence_items_run_id",
        ),
        Index(
            "ix_evidence_items_run_kind_observed",
            "investigation_run_id",
            "kind",
            text("observed_at DESC"),
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    evidence_key: Mapped[str] = mapped_column(String(100), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    summary: Mapped[str] = mapped_column(String(500), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SpecialistReportRow(Base):
    """专业调查节点提交的结构化报告。"""

    __tablename__ = "specialist_reports"
    __table_args__ = (
        CheckConstraint(
            "specialist IN ('log', 'metric', 'change')",
            name="ck_specialist_reports_specialist",
        ),
        CheckConstraint(
            "jsonb_typeof(findings) = 'array' AND jsonb_array_length(findings) BETWEEN 1 AND 20",
            name="ck_specialist_reports_findings_array",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_specialist_reports_confidence",
        ),
        UniqueConstraint(
            "investigation_run_id",
            "specialist",
            name="uq_specialist_reports_run_specialist",
        ),
        UniqueConstraint(
            "investigation_run_id",
            "id",
            name="uq_specialist_reports_run_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    specialist: Mapped[str] = mapped_column(String(16), nullable=False)
    summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    findings: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SpecialistReportEvidenceRow(Base):
    """在数据库层保证报告只能关联同一次调查的证据。"""

    __tablename__ = "specialist_report_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["investigation_run_id", "specialist_report_id"],
            ["specialist_reports.investigation_run_id", "specialist_reports.id"],
            ondelete="RESTRICT",
            name="fk_report_evidence_same_run_report",
        ),
        ForeignKeyConstraint(
            ["investigation_run_id", "evidence_item_id"],
            ["evidence_items.investigation_run_id", "evidence_items.id"],
            ondelete="RESTRICT",
            name="fk_report_evidence_same_run_item",
        ),
    )

    investigation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
    )
    specialist_report_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
    )
    evidence_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
    )


class HypothesisRow(Base):
    """一次调查的主根因假设及确定性质疑状态。"""

    __tablename__ = "hypotheses"
    __table_args__ = (
        CheckConstraint(
            "root_cause IN ('release_500_regression', 'database_pool_misconfiguration', "
            "'redis_unavailable', 'undetermined')",
            name="ck_hypotheses_root_cause",
        ),
        CheckConstraint(
            "status IN ('supported', 'contested', 'insufficient_evidence')",
            name="ck_hypotheses_status",
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_hypotheses_confidence",
        ),
        UniqueConstraint(
            "investigation_run_id",
            name="uq_hypotheses_run",
        ),
        UniqueConstraint(
            "investigation_run_id",
            "id",
            name="uq_hypotheses_run_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        ForeignKey("investigation_runs.id", ondelete="RESTRICT"),
        nullable=False,
    )
    root_cause: Mapped[str] = mapped_column(String(64), nullable=False)
    statement: Mapped[str] = mapped_column(String(1000), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    challenge_summary: Mapped[str] = mapped_column(String(1000), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HypothesisEvidenceRow(Base):
    """证据对同一次调查主根因假设的支持、反驳或上下文关系。"""

    __tablename__ = "hypothesis_evidence"
    __table_args__ = (
        CheckConstraint(
            "relation IN ('supports', 'refutes', 'context')",
            name="ck_hypothesis_evidence_relation",
        ),
        ForeignKeyConstraint(
            ["investigation_run_id", "hypothesis_id"],
            ["hypotheses.investigation_run_id", "hypotheses.id"],
            ondelete="RESTRICT",
            name="fk_hypothesis_evidence_same_run_hypothesis",
        ),
        ForeignKeyConstraint(
            ["investigation_run_id", "evidence_item_id"],
            ["evidence_items.investigation_run_id", "evidence_items.id"],
            ondelete="RESTRICT",
            name="fk_hypothesis_evidence_same_run_item",
        ),
    )

    investigation_run_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
    )
    hypothesis_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
    )
    evidence_item_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True),
        primary_key=True,
    )
    relation: Mapped[str] = mapped_column(String(16), nullable=False)
    rule_code: Mapped[str] = mapped_column(String(100), nullable=False)
    rationale: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
