"""候选命令、完整冻结来源与独立任务契约。"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.model_contracts import (
    ModelAllocation,
    ModelBudgetPolicy,
    ModelInput,
)
from opsswarm.investigation.candidate.output import CandidateEvaluation
from opsswarm.investigation.handoff.contracts import TerminalHandoffRecord
from opsswarm.investigation.human_prepare.contracts import PreparationAttemptView, PreparationClaim
from opsswarm.investigation.human_routing.contracts import HumanRoutingRecord
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitEventRecord, HumanWaitModel
from opsswarm.investigation.models import EvidenceObservation
from opsswarm.investigation.review.contracts import HypothesisReviewProposal
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord
from opsswarm.investigation.synthesis.contracts import OpenHypothesisRecord

POLICY_VERSION: Literal["candidate-evaluation-v1"] = "candidate-evaluation-v1"
PROMPT_VERSION: Literal["candidate-assessment-v1"] = "candidate-assessment-v1"
SourceKind = Literal["terminal_handoff", "human_route"]


class EvaluateCandidateCommand(HumanWaitModel):
    action: Literal["evaluate_candidate"] = "evaluate_candidate"
    source_kind: SourceKind
    source_id: UUID
    source_sha256: Hash
    expected_wait_version: int | None = Field(ge=2, le=16, strict=True)
    idempotency_key: str = Field(
        min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def validate_version(self) -> Self:
        if (self.source_kind == "human_route") != (self.expected_wait_version is not None):
            raise ValueError("仅人工候选来源需要精确等待版本")
        return self


class CandidateSource(HumanWaitModel):
    version: Literal["candidate-source-v1"] = "candidate-source-v1"
    source_kind: SourceKind
    source_id: UUID
    source_sha256: Hash
    service_name: str = Field(min_length=1, max_length=100)
    handoff: TerminalHandoffRecord
    original_route: ReviewRoutingRecord
    human_route: HumanRoutingRecord | None
    human_inputs: tuple[HumanWaitEventRecord, ...] = Field(max_length=15)
    hypothesis: OpenHypothesisRecord
    review: HypothesisReviewProposal
    prior_input: ModelInput
    evidence: tuple[EvidenceObservation, ...] = Field(max_length=100)
    call_budget: CallBudgetPolicy
    model_budget: ModelBudgetPolicy
    started_at: AwareDatetime


class CandidateManifest(HumanWaitModel):
    version: Literal["candidate-context-v1"] = "candidate-context-v1"
    policy_version: Literal["candidate-evaluation-v1"] = POLICY_VERSION
    prompt_version: Literal["candidate-assessment-v1"] = PROMPT_VERSION
    source_sha256: Hash
    input_sha256: Hash
    capability_catalog_version: Literal["action-candidate-catalog-v1"] = (
        "action-candidate-catalog-v1"
    )
    tool_names: tuple[str, ...] = Field(default=(), max_length=0)
    trimming: tuple[str, ...] = Field(default=(), max_length=0)


class CandidatePacket(HumanWaitModel):
    model_input: ModelInput
    allocation: ModelAllocation
    manifest: CandidateManifest


class CandidateClaim(PreparationClaim):
    pass


class CandidateResultView(HumanWaitModel):
    source_call_id: UUID
    content: CandidateEvaluation
    content_sha256: Hash
    created_at: AwareDatetime


class CandidateJobView(HumanWaitModel):
    id: UUID
    investigation_run_id: UUID
    source_kind: SourceKind
    source_id: UUID
    source_sha256: Hash
    wait_version: int | None
    policy_version: Literal["candidate-evaluation-v1"]
    requested_by_id: str
    authorization_id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    last_error_code: str | None
    attempts: tuple[PreparationAttemptView, ...] = Field(max_length=3)
    result: CandidateResultView | None
    availability: Literal["pending", "failed", "current", "stale"]


class CandidateSubmissionResult(HumanWaitModel):
    applied: bool
    job: CandidateJobView


class CandidateAuditRecord(HumanWaitModel):
    cursor: int
    authorization_id: UUID
    investigation_run_id: UUID
    subject_id: str
    command: EvaluateCandidateCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    job_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: AwareDatetime


class CandidateAuditPage(HumanWaitModel):
    items: tuple[CandidateAuditRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class CandidateError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
