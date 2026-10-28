"""不可变调用预算、固定操作和账本读取契约。"""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import Field

from opsswarm.investigation.budget.model_contracts import (
    ModelAllocation,
    ModelBudgetPolicy,
    ModelBudgetUsage,
    ReportedModelUsage,
)
from opsswarm.investigation.tasks.contracts import TaskModel

CallErrorCode = Literal[
    "operation_failed",
    "operation_cancelled",
    "run_deadline_exceeded",
    "telemetry_mcp_invalid_request",
    "telemetry_mcp_timeout",
    "telemetry_mcp_unavailable",
    "telemetry_mcp_protocol_error",
    "telemetry_mcp_invalid_result",
    "telemetry_mcp_tool_error",
]


class CallBudgetPolicy(TaskModel):
    version: Literal["call-count-v1"] = "call-count-v1"
    max_model_calls: int = Field(default=12, ge=1, le=60, strict=True)
    max_tool_calls: int = Field(default=18, ge=1, le=100, strict=True)
    max_tool_calls_per_task: int = Field(default=6, ge=1, le=6, strict=True)
    max_duration_seconds: int = Field(default=300, ge=1, le=1800, strict=True)


class CallOperation(StrEnum):
    COORDINATE = "coordinate"
    COORDINATE_REINVESTIGATION = "coordinate_reinvestigation"
    COLLECT_REINVESTIGATION = "collect_reinvestigation"
    ANALYZE_REINVESTIGATION = "analyze_reinvestigation"
    SYNTHESIZE_REINVESTIGATION = "synthesize_reinvestigation"
    REVIEW_REINVESTIGATION = "review_reinvestigation"
    QUERY_LOGS = "query_logs"
    QUERY_METRICS = "query_metrics"
    LIST_CHANGES = "list_changes"
    ANALYZE_LOG = "analyze_log"
    ANALYZE_METRIC = "analyze_metric"
    ANALYZE_CHANGE = "analyze_change"
    SYNTHESIZE = "synthesize"
    REVIEW = "review"
    HUMAN_REVIEW = "human_review"
    EVALUATE_CANDIDATE = "evaluate_candidate"
    READ_LOG_EVIDENCE = "read_log_evidence"
    READ_METRIC_EVIDENCE = "read_metric_evidence"
    READ_CHANGE_EVIDENCE = "read_change_evidence"
    READ_LOG_RUNBOOK = "read_log_runbook"
    READ_METRIC_RUNBOOK = "read_metric_runbook"
    READ_CHANGE_RUNBOOK = "read_change_runbook"


class CallReservationRecord(TaskModel):
    id: UUID
    investigation_run_id: UUID
    investigation_attempt_id: UUID
    human_review_attempt_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    candidate_attempt_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    reinvestigation_attempt_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    reflow_task_id: UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    reflow_task_attempt_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    reflow_review_attempt_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    reflow_synthesis_attempt_id: UUID | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    task_id: UUID | None
    task_attempt_id: UUID | None
    sequence: int
    invocation_index: int = Field(default=0, ge=0, le=99, strict=True)
    operation: CallOperation
    kind: Literal["model", "tool"]
    status: Literal["reserved", "succeeded", "failed", "unknown"]
    error_code: str | None
    reserved_at: datetime
    finished_at: datetime | None
    model_allocation: ModelAllocation | None = None
    model_usage: ReportedModelUsage | None = None

    @property
    def scope_task_id(self) -> UUID | None:
        """旧任务与独立回流任务使用各自稳定业务身份累计工具额度。"""
        return self.task_id or self.reflow_task_id


@dataclass(frozen=True, slots=True)
class CallGrant:
    record: CallReservationRecord
    created: bool
    remaining_seconds: float


class CallBudgetSnapshot(TaskModel):
    investigation_run_id: UUID
    policy: CallBudgetPolicy | None
    started_at: datetime | None
    deadline: datetime | None
    reserved_model_calls: int | None
    reserved_tool_calls: int | None
    calls: tuple[CallReservationRecord, ...]
    model_budget: ModelBudgetPolicy | None = None
    model_usage: ModelBudgetUsage | None = None
