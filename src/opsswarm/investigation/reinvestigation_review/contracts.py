"""回流审查的冻结材料、独立尝试和已记录意见契约。"""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from opsswarm.investigation.reinvestigation_coordination.contracts import (
    ReflowError as ReflowReviewError,
)

__all__ = ["ReflowReviewError"]

from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.model_contracts import (
    ModelAllocation,
    ModelBudgetPolicy,
    ModelInput,
)
from opsswarm.investigation.coordination.contracts import CoordinationPolicy
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel
from opsswarm.investigation.reinvestigation_dispatch.contracts import DispatchContent
from opsswarm.investigation.reinvestigation_execution.contracts import (
    EffectiveWindow,
    ExecutionResult,
)
from opsswarm.investigation.reinvestigation_review.output import (
    ReflowReviewModelResult as ReflowReviewModelResult,
)
from opsswarm.investigation.reinvestigation_synthesis.contracts import SynthesisResult
from opsswarm.investigation.review.contracts import HypothesisReviewProposal
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot
from opsswarm.verification.candidate.contracts import UtcTime

POLICY_VERSION: Literal["reinvestigation-review-v1"] = "reinvestigation-review-v1"
PROMPT_VERSION: Literal["reinvestigation-review-v1"] = "reinvestigation-review-v1"
OPERATION = "review_reinvestigation"
ROUTE = "/api/v1/investigations/{run_id}/reinvestigation-review"


class ReflowReviewMaterial(HumanWaitModel):
    kind: str = Field(min_length=1, max_length=50)
    reference_id: str = Field(min_length=1, max_length=100)
    content_sha256: Hash
    content: dict[str, Any]


class ReflowReviewProfessional(HumanWaitModel):
    task_id: UUID
    assignment: TaskAssignment
    effective_window: EffectiveWindow
    evidence: TaskEvidenceSnapshot
    result: ExecutionResult


class ReflowReviewSource(HumanWaitModel):
    version: Literal["reinvestigation-review-source-v1"] = "reinvestigation-review-source-v1"
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
    materials: tuple[ReflowReviewMaterial, ...] = Field(min_length=10, max_length=10)
    coordination_created_at: UtcTime
    coordination_source_sha256: Hash
    dispatch: DispatchContent
    dispatch_sha256: Hash
    professionals: tuple[ReflowReviewProfessional, ...] = Field(min_length=1, max_length=3)
    synthesis_created_at: UtcTime
    synthesis_source_sha256: Hash
    synthesis_packet_sha256: Hash
    synthesis_result: SynthesisResult
    synthesis_result_sha256: Hash


class ReflowReviewManifest(HumanWaitModel):
    version: Literal["reinvestigation-review-context-v1"] = "reinvestigation-review-context-v1"
    policy_version: Literal["reinvestigation-review-v1"] = POLICY_VERSION
    prompt_version: Literal["reinvestigation-review-v1"] = PROMPT_VERSION
    source_sha256: Hash
    input_sha256: Hash
    professional_hashes: tuple[Hash, ...] = Field(min_length=1, max_length=3)
    synthesis_result_sha256: Hash
    role_version: Literal["reinvestigation-reviewer-v1"] = "reinvestigation-reviewer-v1"
    tool_names: tuple[str, ...] = Field(default=(), max_length=0)
    trimming: tuple[str, ...] = Field(default=(), max_length=0)


class ReflowReviewPacket(HumanWaitModel):
    model_input: ModelInput
    allocation: ModelAllocation
    manifest: ReflowReviewManifest


class ReflowReviewClaim(HumanWaitModel):
    job_id: UUID
    investigation_run_id: UUID
    attempt_id: UUID
    number: int
    lease_token: UUID
    lease_expires_at: UtcTime


class ReflowReviewResult(HumanWaitModel):
    investigation_run_id: UUID
    job_id: UUID
    source_call_id: UUID
    source_sha256: Hash
    proposal: HypothesisReviewProposal
    status: Literal["recorded"] = "recorded"
    synthesis_result_sha256: Hash
    created_at: UtcTime


class ReflowReviewAttempt(HumanWaitModel):
    id: UUID
    number: int
    status: str
    started_at: UtcTime
    finished_at: UtcTime | None
    error_code: str | None


class ReflowReviewView(HumanWaitModel):
    job_id: UUID | None
    status: Literal["pending", "queued", "running", "succeeded", "blocked", "failed"]
    source_sha256: Hash | None
    packet: ReflowReviewPacket | None
    attempts: tuple[ReflowReviewAttempt, ...] = Field(max_length=3)
    result: ReflowReviewResult | None
    error_code: str | None
