"""调用预留账本的 PostgreSQL 映射与归属约束。"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PostgreSQLUUID
from sqlalchemy.orm import Mapped, mapped_column

from opsswarm.database.base import Base


class CallReservationRow(Base):
    __tablename__ = "investigation_call_reservations"
    __table_args__ = (
        CheckConstraint(
            "(operation='coordinate_reinvestigation') = (reinvestigation_attempt_id IS NOT NULL)",
            name="ck_call_reservations_reflow_owner",
        ),
        ForeignKeyConstraint(
            ["reinvestigation_attempt_id", "investigation_run_id"],
            [
                "investigation_reflow_attempts.id",
                "investigation_reflow_attempts.investigation_run_id",
            ],
            name="fk_call_reservations_reflow_attempt",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        CheckConstraint(
            "(operation='evaluate_candidate') = (candidate_attempt_id IS NOT NULL)",
            name="ck_call_reservations_candidate_owner",
        ),
        ForeignKeyConstraint(
            ["candidate_attempt_id", "investigation_run_id"],
            [
                "investigation_candidate_evaluation_attempts.id",
                "investigation_candidate_evaluation_attempts.investigation_run_id",
            ],
            name="fk_call_reservations_candidate_attempt",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        CheckConstraint(
            "(operation='human_review') = (human_review_attempt_id IS NOT NULL)",
            name="ck_call_reservations_human_owner",
        ),
        ForeignKeyConstraint(
            ["human_review_attempt_id", "investigation_run_id"],
            [
                "investigation_human_review_attempts.id",
                "investigation_human_review_attempts.investigation_run_id",
            ],
            name="fk_call_reservations_human_attempt",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        CheckConstraint(
            "((operation IN ('read_log_evidence','read_metric_evidence',"
            "'read_change_evidence','read_log_runbook','read_metric_runbook',"
            "'read_change_runbook')) = (tool_request IS NOT NULL)) AND (tool_request "
            "IS NULL OR (jsonb_typeof(tool_request) = 'object' AND "
            "octet_length(tool_request::text) <= 8192)) AND (call_result IS NULL OR "
            "(status <> 'reserved' AND jsonb_typeof(call_result) = 'object' AND "
            "octet_length(call_result::text) <= 65536 AND COALESCE(call_result ->> "
            "'kind', '') = kind AND ((kind = 'model' AND ((operation = 'coordinate' "
            "AND COALESCE(model_input ->> 'version', '') IN "
            "('coordination-model-input-v1','coordination-experience-input-v1') AND "
            "COALESCE(call_result ->> 'version', '') = "
            "'coordination-model-result-v1') OR (operation = 'synthesize' AND NOT "
            "(model_input ? 'version') AND COALESCE(call_result ->> 'version', '') = "
            "'open-hypothesis-synthesis-result-v1') OR (operation IN ('review', "
            "'human_review') AND NOT (model_input ? 'version') AND "
            "COALESCE(call_result ->> 'version', '') = "
            "'hypothesis-review-result-v1') OR (operation = 'evaluate_candidate' "
            "AND NOT (model_input ? 'version') AND COALESCE(call_result ->> "
            "'version', '') = 'candidate-model-result-v1') OR "
            "(operation='coordinate_reinvestigation' AND NOT (model_input ? 'version') "
            "AND COALESCE(call_result ->> 'version', '') = "
            "'reinvestigation-coordination-result-v1') OR (operation NOT IN ('coordinate', "
            "'synthesize', 'review', 'human_review', 'evaluate_candidate', "
            "'coordinate_reinvestigation') AND "
            "COALESCE(model_input ->> "
            "'version', '') IN ('evidence-loop-input-v1','runbook-loop-input-v1')))) "
            "OR (kind = 'tool' AND tool_request IS NOT NULL))))",
            name="ck_call_reservations_loop_payload",
        ),
        CheckConstraint(
            "invocation_index BETWEEN 0 AND 99 AND (operation <> 'coordinate' OR "
            "invocation_index = 0) AND (operation <> 'review' OR invocation_index IN "
            "(0, 1, 2)) AND (operation NOT IN ('human_review', 'evaluate_candidate', "
            "'coordinate_reinvestigation') "
            "OR invocation_index = 0)",
            name="ck_call_reservations_invocation_index",
        ),
        CheckConstraint(
            "CASE WHEN operation='human_review' THEN idempotency_key='human-review:' "
            "|| human_review_attempt_id::text WHEN operation='evaluate_candidate' THEN "
            "idempotency_key='candidate:' || candidate_attempt_id::text WHEN "
            "operation='coordinate_reinvestigation' THEN idempotency_key='reflow:' || "
            "reinvestigation_attempt_id::text ELSE idempotency_key = "
            "investigation_attempt_id::text || ':' || COALESCE(task_attempt_id::text,"
            " 'root') || ':' || operation || CASE WHEN invocation_index = 0 THEN '' "
            "ELSE ':' || invocation_index::text END END",
            name="ck_call_reservations_invocation_key",
        ),
        CheckConstraint(
            "context_manifest IS NULL OR (model_input IS NOT NULL AND kind = 'model' "
            "AND jsonb_typeof(context_manifest) = 'object' AND "
            "octet_length(context_manifest::text) <= 32768)",
            name="ck_call_reservations_context_manifest",
        ),
        UniqueConstraint(
            "id",
            "task_id",
            "task_attempt_id",
            "investigation_run_id",
            name="uq_call_reservations_evidence_scope",
        ),
        UniqueConstraint("id", "investigation_run_id", name="uq_call_reservations_id_run"),
        CheckConstraint(
            "(model_input IS NULL) = (model_allocation IS NULL) AND "
            "(model_allocation IS NULL OR (kind = 'model' AND "
            "jsonb_typeof(model_input) = 'object' AND "
            "octet_length(model_input::text) <= 524288 AND "
            "jsonb_typeof(model_allocation) = 'object' AND "
            "octet_length(model_allocation::text) <= 8192)) AND (model_usage IS NULL "
            "OR (model_allocation IS NOT NULL AND status <> 'reserved' AND "
            "jsonb_typeof(model_usage) = 'object' AND "
            "octet_length(model_usage::text) <= 8192))",
            name="ck_call_reservations_model_budget",
        ),
        ForeignKeyConstraint(
            ["investigation_attempt_id", "investigation_run_id"],
            ["investigation_attempts.id", "investigation_attempts.investigation_run_id"],
            ondelete="RESTRICT",
            name="fk_call_reservations_parent",
        ),
        ForeignKeyConstraint(
            ["task_attempt_id", "task_id", "investigation_run_id"],
            [
                "agent_task_attempts.id",
                "agent_task_attempts.task_id",
                "agent_task_attempts.investigation_run_id",
            ],
            ondelete="RESTRICT",
            name="fk_call_reservations_task",
        ),
        UniqueConstraint("investigation_run_id", "sequence", name="uq_call_reservations_sequence"),
        UniqueConstraint(
            "investigation_run_id", "idempotency_key", name="uq_call_reservations_key"
        ),
        CheckConstraint("sequence BETWEEN 1 AND 160", name="ck_call_reservations_sequence"),
        CheckConstraint(
            "status IN ('reserved', 'succeeded', 'failed', 'unknown')",
            name="ck_call_reservations_status",
        ),
        CheckConstraint(
            "(status = 'reserved') = (finished_at IS NULL) AND finished_at >= reserved_at",
            name="ck_call_reservations_time",
        ),
        CheckConstraint(
            "(status IN ('failed', 'unknown')) = (error_code IS NOT NULL)",
            name="ck_call_reservations_error",
        ),
        CheckConstraint(
            "(operation IN ('query_logs', 'query_metrics', 'list_changes', "
            "'read_log_evidence', 'read_metric_evidence', 'read_change_evidence', "
            "'read_log_runbook', 'read_metric_runbook', 'read_change_runbook') AND "
            "kind = 'tool') OR (operation IN ('coordinate', 'analyze_log', "
            "'analyze_metric', 'analyze_change', 'synthesize', 'review', "
            "'human_review', 'evaluate_candidate', 'coordinate_reinvestigation') AND kind"
            " = 'model')",
            name="ck_call_reservations_operation",
        ),
        CheckConstraint(
            "(operation IN ('coordinate', 'synthesize', 'review', 'human_review', "
            "'evaluate_candidate', 'coordinate_reinvestigation') "
            "AND task_id IS NULL AND task_attempt_id IS NULL) OR (operation NOT IN "
            "('coordinate', 'synthesize', 'review', 'human_review', 'evaluate_candidate',"
            " 'coordinate_reinvestigation') "
            "AND task_id IS "
            "NOT NULL AND task_attempt_id IS NOT NULL)",
            name="ck_call_reservations_scope",
        ),
    )

    id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), primary_key=True)
    investigation_run_id: Mapped[UUID] = mapped_column(PostgreSQLUUID(as_uuid=True), nullable=False)
    investigation_attempt_id: Mapped[UUID] = mapped_column(
        PostgreSQLUUID(as_uuid=True), nullable=False
    )
    reinvestigation_attempt_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    candidate_attempt_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    human_review_attempt_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    task_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    task_attempt_id: Mapped[UUID | None] = mapped_column(PostgreSQLUUID(as_uuid=True))
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    invocation_index: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    idempotency_key: Mapped[str] = mapped_column(String(120), nullable=False)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error_code: Mapped[str | None] = mapped_column(String(100))
    reserved_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    context_manifest: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    model_input: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    model_allocation: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    model_usage: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    tool_request: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
    call_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB(none_as_null=True))
