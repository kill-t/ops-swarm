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


class CallBudgetPolicy(TaskModel):
    version: Literal["call-count-v1"] = "call-count-v1"
    max_model_calls: int = Field(default=12, ge=1, le=60, strict=True)
    max_tool_calls: int = Field(default=18, ge=1, le=100, strict=True)
    max_tool_calls_per_task: int = Field(default=6, ge=1, le=6, strict=True)
    max_duration_seconds: int = Field(default=300, ge=1, le=1800, strict=True)


class CallOperation(StrEnum):
    QUERY_LOGS = "query_logs"
    QUERY_METRICS = "query_metrics"
    LIST_CHANGES = "list_changes"
    ANALYZE_LOG = "analyze_log"
    ANALYZE_METRIC = "analyze_metric"
    ANALYZE_CHANGE = "analyze_change"
    SYNTHESIZE = "synthesize"
    READ_LOG_EVIDENCE = "read_log_evidence"
    READ_METRIC_EVIDENCE = "read_metric_evidence"
    READ_CHANGE_EVIDENCE = "read_change_evidence"


class CallReservationRecord(TaskModel):
    id: UUID
    investigation_run_id: UUID
    investigation_attempt_id: UUID
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
