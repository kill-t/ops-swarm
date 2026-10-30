"""精确回流候选来源、上下文及独立持久任务契约。"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.model_contracts import (
    ModelAllocation,
    ModelBudgetPolicy,
    ModelInput,
)
from opsswarm.investigation.candidate.output import CandidateEvaluation
from opsswarm.investigation.candidate.output import (
    CandidateModelResult as ReflowCandidateModelResult,
)
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel
from opsswarm.investigation.reinvestigation_coordination.contracts import (
    ReflowError as ReflowCandidateError,
)
from opsswarm.investigation.reinvestigation_review.contracts import (
    ReflowReviewAttempt as ReflowCandidateAttempt,
)
from opsswarm.investigation.reinvestigation_review.contracts import (
    ReflowReviewClaim as ReflowCandidateClaim,
)
from opsswarm.investigation.reinvestigation_review.contracts import (
    ReflowReviewResult,
    ReflowReviewSource,
)
from opsswarm.investigation.reinvestigation_routing.contracts import ReflowRoutingContent
from opsswarm.verification.candidate.contracts import UtcTime

__all__ = [
    "ReflowCandidateModelResult",
    "ReflowCandidateError",
    "ReflowCandidateAttempt",
    "ReflowCandidateClaim",
]

POLICY_VERSION: Literal["reinvestigation-candidate-v1"] = "reinvestigation-candidate-v1"
PROMPT_VERSION: Literal["reinvestigation-candidate-v1"] = "reinvestigation-candidate-v1"
OPERATION = "evaluate_reinvestigation_candidate"
ROUTE = "/api/v1/investigations/{run_id}/reinvestigation-candidate"


class ReflowCandidateSource(HumanWaitModel):
    version: Literal["reinvestigation-candidate-source-v1"] = "reinvestigation-candidate-source-v1"
    investigation_run_id: UUID
    incident_id: UUID
    parent_attempt_id: UUID
    call_budget: CallBudgetPolicy
    model_budget: ModelBudgetPolicy
    started_at: UtcTime
    created_at: UtcTime
    service_name: str = Field(min_length=1, max_length=100)
    review_source: ReflowReviewSource
    review_result: ReflowReviewResult
    routing: ReflowRoutingContent
    routing_sha256: Hash


class ReflowCandidateManifest(HumanWaitModel):
    version: Literal["reinvestigation-candidate-context-v1"] = (
        "reinvestigation-candidate-context-v1"
    )
    policy_version: Literal["reinvestigation-candidate-v1"] = POLICY_VERSION
    prompt_version: Literal["reinvestigation-candidate-v1"] = PROMPT_VERSION
    source_sha256: Hash
    input_sha256: Hash
    routing_sha256: Hash
    role_version: Literal["reinvestigation-candidate-evaluator-v1"] = (
        "reinvestigation-candidate-evaluator-v1"
    )
    capability_catalog_version: Literal["action-candidate-catalog-v1"] = (
        "action-candidate-catalog-v1"
    )
    tool_names: tuple[str, ...] = Field(default=(), max_length=0)
    trimming: tuple[str, ...] = Field(default=(), max_length=0)


class ReflowCandidatePacket(HumanWaitModel):
    model_input: ModelInput
    allocation: ModelAllocation
    manifest: ReflowCandidateManifest


class ReflowCandidateResult(HumanWaitModel):
    version: Literal["reinvestigation-candidate-result-v1"] = "reinvestigation-candidate-result-v1"
    investigation_run_id: UUID
    job_id: UUID
    source_call_id: UUID
    source_sha256: Hash
    routing_sha256: Hash
    evaluation: CandidateEvaluation
    status: Literal["recorded"] = "recorded"
    created_at: UtcTime


class ReflowCandidateView(HumanWaitModel):
    job_id: UUID | None
    status: Literal["pending", "queued", "running", "succeeded", "blocked", "failed"]
    source_sha256: Hash | None
    packet: ReflowCandidatePacket | None
    attempts: tuple[ReflowCandidateAttempt, ...] = Field(max_length=3)
    result: ReflowCandidateResult | None
    error_code: str | None
