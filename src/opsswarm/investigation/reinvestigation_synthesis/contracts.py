"""回流综合的冻结材料、独立尝试和待审查结果契约。"""

from typing import Any, Literal
from uuid import UUID

from pydantic import Field

from opsswarm.investigation.reinvestigation_coordination.contracts import (
    ReflowError as SynthesisError,
)

__all__ = ["SynthesisError"]

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
from opsswarm.investigation.reinvestigation_synthesis.output import (
    SynthesisModelResult as SynthesisModelResult,
)
from opsswarm.investigation.synthesis.contracts import OpenHypothesisProposal
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot
from opsswarm.verification.candidate.contracts import UtcTime

POLICY_VERSION: Literal["reinvestigation-synthesis-v1"] = "reinvestigation-synthesis-v1"
PROMPT_VERSION: Literal["reinvestigation-synthesize-v1"] = "reinvestigation-synthesize-v1"
OPERATION = "synthesize_reinvestigation"
ROUTE = "/api/v1/investigations/{run_id}/reinvestigation-synthesis"


class SynthesisMaterial(HumanWaitModel):
    kind: str = Field(min_length=1, max_length=50)
    reference_id: str = Field(min_length=1, max_length=100)
    content_sha256: Hash
    content: dict[str, Any]


class SynthesisProfessional(HumanWaitModel):
    task_id: UUID
    assignment: TaskAssignment
    effective_window: EffectiveWindow
    evidence: TaskEvidenceSnapshot
    result: ExecutionResult


class SynthesisSource(HumanWaitModel):
    version: Literal["reinvestigation-synthesis-source-v1"] = "reinvestigation-synthesis-source-v1"
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
    materials: tuple[SynthesisMaterial, ...] = Field(min_length=10, max_length=10)
    coordination_created_at: UtcTime
    coordination_source_sha256: Hash
    dispatch: DispatchContent
    dispatch_sha256: Hash
    professionals: tuple[SynthesisProfessional, ...] = Field(min_length=1, max_length=3)


class SynthesisManifest(HumanWaitModel):
    version: Literal["reinvestigation-synthesis-context-v1"] = (
        "reinvestigation-synthesis-context-v1"
    )
    policy_version: Literal["reinvestigation-synthesis-v1"] = POLICY_VERSION
    prompt_version: Literal["reinvestigation-synthesize-v1"] = PROMPT_VERSION
    source_sha256: Hash
    input_sha256: Hash
    professional_hashes: tuple[Hash, ...] = Field(min_length=1, max_length=3)
    tool_names: tuple[str, ...] = Field(default=(), max_length=0)
    trimming: tuple[str, ...] = Field(default=(), max_length=0)


class SynthesisPacket(HumanWaitModel):
    model_input: ModelInput
    allocation: ModelAllocation
    manifest: SynthesisManifest


class SynthesisClaim(HumanWaitModel):
    job_id: UUID
    investigation_run_id: UUID
    attempt_id: UUID
    number: int
    lease_token: UUID
    lease_expires_at: UtcTime


class SynthesisResult(HumanWaitModel):
    investigation_run_id: UUID
    job_id: UUID
    source_call_id: UUID
    source_sha256: Hash
    proposal: OpenHypothesisProposal
    review_status: Literal["pending_review"] = "pending_review"
    created_at: UtcTime


class SynthesisAttempt(HumanWaitModel):
    id: UUID
    number: int
    status: str
    started_at: UtcTime
    finished_at: UtcTime | None
    error_code: str | None


class SynthesisView(HumanWaitModel):
    job_id: UUID | None
    status: Literal["pending", "queued", "running", "succeeded", "blocked", "failed"]
    source_sha256: Hash | None
    packet: SynthesisPacket | None
    attempts: tuple[SynthesisAttempt, ...] = Field(max_length=3)
    result: SynthesisResult | None
    error_code: str | None
