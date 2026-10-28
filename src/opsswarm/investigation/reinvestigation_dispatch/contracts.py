"""独立回流分派事实与待执行任务的严格契约。"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel
from opsswarm.investigation.tasks.contracts import RoundAssignment, TaskAssignment
from opsswarm.verification.candidate.contracts import UtcTime

POLICY_VERSION = "reinvestigation-dispatch-v1"
ROUTE = "/api/v1/investigations/{run_id}/reinvestigation-dispatch"


class DispatchContent(HumanWaitModel):
    version: Literal["reinvestigation-dispatch-v1"] = "reinvestigation-dispatch-v1"
    investigation_run_id: UUID
    source_result_sha256: Hash
    source_sha256: Hash
    source_call_id: UUID
    basis_event_cursor: int = Field(ge=1, strict=True)
    service_name: str = Field(min_length=1, max_length=100)
    evidence_policy_version: Literal["post-verification-read-only-v1"] = (
        "post-verification-read-only-v1"
    )
    execution_policy_version: Literal["reinvestigation-task-v1"] = "reinvestigation-task-v1"
    observation_floor_at: UtcTime
    budget: CallBudgetSnapshot
    status: Literal["queued", "blocked"]
    reason_code: str | None
    round_id: UUID | None
    assignment: RoundAssignment | None
    created_at: UtcTime


class DispatchTask(HumanWaitModel):
    id: UUID
    round_id: UUID
    investigation_run_id: UUID
    task_key: str
    status: Literal["pending"] = "pending"
    assignment: TaskAssignment
    assignment_sha256: Hash
    created_at: UtcTime


class DispatchView(HumanWaitModel):
    record: DispatchContent | None
    tasks: tuple[DispatchTask, ...] = Field(max_length=3)
    availability: Literal["pending", "current", "stale", "expired", "blocked"]
    blocking_reason_codes: tuple[str, ...]
