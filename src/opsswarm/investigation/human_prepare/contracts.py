"""准备命令、完整来源、输入工件与独立任务读取契约。"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallBudgetSnapshot
from opsswarm.investigation.budget.model_contracts import (
    ModelAllocation,
    ModelBudgetPolicy,
    ModelInput,
)
from opsswarm.investigation.context.contracts import ContextManifest
from opsswarm.investigation.handoff.contracts import TerminalHandoffRecord
from opsswarm.investigation.human_wait.contracts import (
    Hash,
    HumanWaitModel,
    HumanWaitState,
    WaitCommandBase,
)
from opsswarm.investigation.review.contracts import HypothesisReviewRecord
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord

POLICY_VERSION = "human-input-preparation-v1"
PROMPT_VERSION = "human-input-review-preparation-v1"


class PrepareCommand(WaitCommandBase):
    action: Literal["prepare"] = "prepare"


class PreparationSource(HumanWaitModel):
    version: Literal["human-preparation-source-v1"] = "human-preparation-source-v1"
    handoff: TerminalHandoffRecord
    wait: HumanWaitState
    route: ReviewRoutingRecord
    review: HypothesisReviewRecord
    prior_input: ModelInput
    prior_manifest: ContextManifest
    call_budget: CallBudgetPolicy
    model_budget: ModelBudgetPolicy
    started_at: AwareDatetime

    @model_validator(mode="after")
    def validate_scope(self) -> "PreparationSource":
        run_id = self.handoff.investigation_run_id
        if (
            self.wait.investigation_run_id != run_id
            or self.route.investigation_run_id != run_id
            or self.review.investigation_run_id != run_id
            or self.prior_manifest.investigation_run_id != run_id
            or self.handoff.destination != "await_human"
            or self.wait.status != "waiting"
            or not self.wait.events
            or len(self.wait.events) > 15
            or self.wait.state_version != len(self.wait.events) + 1
            or any(event.action != "supplement" for event in self.wait.events)
            or self.wait.handoff_id != self.handoff.id
            or self.wait.source_sha256 != self.handoff.source_sha256
            or self.route.id != self.handoff.source_review_route_id
            or self.route.hypothesis_review_id != self.review.id
            or self.route.content_sha256 != self.handoff.source_sha256
        ):
            raise ValueError("准备来源必须绑定同一人工等待及完整补充前缀")
        return self


class PreparationMaterial(HumanWaitModel):
    kind: Literal["original_review_input", "prior_review", "terminal_route", "human_event"]
    material_id: UUID
    version: str = Field(min_length=1, max_length=100)
    content_sha256: Hash


class PreparationManifest(HumanWaitModel):
    version: Literal["human-preparation-context-v1"] = "human-preparation-context-v1"
    policy_version: Literal["human-input-preparation-v1"] = "human-input-preparation-v1"
    prompt_version: Literal["human-input-review-preparation-v1"] = (
        "human-input-review-preparation-v1"
    )
    workflow_version: Literal["v3"] = "v3"
    graph_version: Literal["dynamic-investigation-graph-v15"] = "dynamic-investigation-graph-v15"
    source_sha256: Hash
    materials: tuple[PreparationMaterial, ...] = Field(min_length=4, max_length=18)
    tool_names: tuple[str, ...] = Field(default=(), max_length=0)
    trimming: tuple[str, ...] = Field(default=(), max_length=0)


class PreparedInput(HumanWaitModel):
    model_input: ModelInput | None
    manifest: PreparationManifest
    allocation: ModelAllocation | None
    input_error_code: str | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def validate_allocation_shape(self) -> "PreparedInput":
        if self.input_error_code is None:
            if self.model_input is None or self.allocation is None:
                raise ValueError("可用输入必须保存完整正文与分配估算")
        elif self.allocation is not None:
            raise ValueError("输入被阻断时不能保留可用分配估算")
        return self


class PreparationDiagnostic(HumanWaitModel):
    status: Literal["ready", "blocked"]
    reason_code: str | None = Field(max_length=100)
    checked_at: AwareDatetime

    @model_validator(mode="after")
    def validate_reason(self) -> "PreparationDiagnostic":
        if (self.status == "ready") != (self.reason_code is None):
            raise ValueError("就绪状态与阻断原因不一致")
        return self


class PreparationArtifact(HumanWaitModel):
    version: Literal["human-preparation-artifact-v1"] = "human-preparation-artifact-v1"
    source_sha256: Hash
    prepared_input: PreparedInput
    budget: CallBudgetSnapshot
    diagnostic: PreparationDiagnostic


class PreparationAttemptView(HumanWaitModel):
    id: UUID
    number: int = Field(ge=1, le=3)
    worker_id: str
    status: Literal["running", "succeeded", "failed", "lease_expired"]
    started_at: AwareDatetime
    lease_expires_at: AwareDatetime
    finished_at: AwareDatetime | None
    error_code: str | None


class PreparationJobView(HumanWaitModel):
    id: UUID
    investigation_run_id: UUID
    wait_version: int = Field(ge=2, le=16)
    policy_version: Literal["human-input-preparation-v1"]
    source_sha256: Hash
    requested_by_id: str
    authorization_id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    last_error_code: str | None
    attempts: tuple[PreparationAttemptView, ...] = Field(max_length=3)
    artifact: PreparationArtifact | None
    availability: Literal["pending", "failed", "ready", "blocked", "stale"]
    availability_reason: str | None


class PreparationResult(HumanWaitModel):
    applied: bool
    job: PreparationJobView


class PreparationAuditRecord(HumanWaitModel):
    cursor: int
    authorization_id: UUID
    investigation_run_id: UUID
    subject_id: str
    command: PrepareCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    job_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: AwareDatetime


class PreparationAuditPage(HumanWaitModel):
    items: tuple[PreparationAuditRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class PreparationError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code


class PreparationClaim(HumanWaitModel):
    job_id: UUID
    investigation_run_id: UUID
    attempt_id: UUID
    number: int
    lease_token: UUID
    lease_expires_at: AwareDatetime
