"""回流协调的冻结材料、独立尝试和待分派结果契约。"""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.model_contracts import (
    ModelAllocation,
    ModelBudgetPolicy,
    ModelInput,
)
from opsswarm.investigation.coordination.contracts import CoordinationPolicy, CoordinationProposal
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel
from opsswarm.investigation.reinvestigation_coordination.output import (
    ReflowModelResult as ReflowModelResult,
)
from opsswarm.verification.candidate.contracts import UtcTime

POLICY_VERSION: Literal["reinvestigation-coordination-v1"] = "reinvestigation-coordination-v1"
PROMPT_VERSION: Literal["reinvestigation-coordinate-v1"] = "reinvestigation-coordinate-v1"
OPERATION = "coordinate_reinvestigation"
ROUTE = "/api/v1/investigations/{run_id}/reinvestigation-coordination"


class ReflowMaterial(HumanWaitModel):
    kind: str = Field(min_length=1, max_length=50)
    reference_id: str = Field(min_length=1, max_length=100)
    content_sha256: Hash
    content: dict[str, Any]


class ReflowSource(HumanWaitModel):
    version: Literal["reinvestigation-coordination-source-v1"] = (
        "reinvestigation-coordination-source-v1"
    )
    investigation_run_id: UUID
    incident_id: UUID
    admission_sha256: Hash
    incident_version: int = Field(ge=1, strict=True)
    parent_attempt_id: UUID
    coordination_policy: CoordinationPolicy
    next_round_number: int = Field(ge=1, le=4, strict=True)
    existing_task_count: int = Field(ge=0, le=9, strict=True)
    maximum_tasks: int = Field(ge=0, le=3, strict=True)
    call_budget: CallBudgetPolicy
    model_budget: ModelBudgetPolicy
    started_at: UtcTime
    created_at: UtcTime
    materials: tuple[ReflowMaterial, ...] = Field(min_length=10, max_length=10)


class ReflowManifest(HumanWaitModel):
    version: Literal["reinvestigation-coordination-context-v1"] = (
        "reinvestigation-coordination-context-v1"
    )
    policy_version: Literal["reinvestigation-coordination-v1"] = POLICY_VERSION
    prompt_version: Literal["reinvestigation-coordinate-v1"] = PROMPT_VERSION
    source_sha256: Hash
    input_sha256: Hash
    materials: tuple[dict[str, str], ...] = Field(min_length=10, max_length=10)
    tool_names: tuple[str, ...] = Field(default=(), max_length=0)
    trimming: tuple[str, ...] = Field(default=(), max_length=0)


class ReflowPacket(HumanWaitModel):
    model_input: ModelInput
    allocation: ModelAllocation
    manifest: ReflowManifest


class ReflowClaim(HumanWaitModel):
    job_id: UUID
    investigation_run_id: UUID
    attempt_id: UUID
    number: int
    lease_token: UUID
    lease_expires_at: UtcTime


class ReflowResult(HumanWaitModel):
    investigation_run_id: UUID
    job_id: UUID
    source_call_id: UUID
    source_sha256: Hash
    proposal: CoordinationProposal
    dispatch_status: Literal["not_dispatched"] = "not_dispatched"
    created_at: UtcTime


class ReflowAttempt(HumanWaitModel):
    id: UUID
    number: int
    status: str
    started_at: UtcTime
    finished_at: UtcTime | None
    error_code: str | None


class ReflowView(HumanWaitModel):
    job_id: UUID | None
    status: Literal["pending", "queued", "running", "succeeded", "blocked", "failed"]
    source_sha256: Hash | None
    packet: ReflowPacket | None
    attempts: tuple[ReflowAttempt, ...] = Field(max_length=3)
    result: ReflowResult | None
    error_code: str | None


class ReflowError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
