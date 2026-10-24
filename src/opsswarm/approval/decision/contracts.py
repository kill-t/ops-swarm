"""候选审批决定的严格命令、不可变内容与查询契约。"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from opsswarm.investigation.candidate.output import CandidateItem, EvidenceId
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel

POLICY_VERSION: Literal["candidate-approval-decision-v1"] = "candidate-approval-decision-v1"
DecisionAction = Literal["approve", "reject", "expire"]
DecisionOutcome = Literal["approved", "rejected", "expired"]
DecisionAvailability = Literal["pending", "current", "stale", "expired", "recorded"]


class DecisionCommand(HumanWaitModel):
    """确认原未决条件的人工判断，不接受新的方案或执行授权。"""

    action: DecisionAction
    request_id: UUID
    plan_content_sha256: Hash
    expected_incident_version: int = Field(ge=1, strict=True)
    acknowledge_unresolved_conditions: bool = Field(default=False, strict=True)
    idempotency_key: str = Field(
        min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_acknowledgement(self) -> Self:
        if self.acknowledge_unresolved_conditions != (self.action == "approve"):
            raise ValueError("仅批准必须明确确认已阅读原前置条件和未解问题")
        return self


class DecisionContent(HumanWaitModel):
    """保留原计划的证据和全部未决约束，决定不提供执行资格。"""

    version: Literal["candidate-approval-decision-content-v1"] = (
        "candidate-approval-decision-content-v1"
    )
    request_id: UUID
    plan_id: UUID
    plan_content_sha256: Hash
    candidate_job_id: UUID
    source_content_sha256: Hash
    wait_version: int | None = Field(ge=2, le=16, strict=True)
    outcome: DecisionOutcome
    evidence_ids: tuple[EvidenceId, ...] = Field(min_length=1, max_length=100)
    preconditions: tuple[CandidateItem, ...] = Field(min_length=1, max_length=10)
    unresolved_questions: tuple[CandidateItem, ...] = Field(max_length=10)
    execution_eligible: Literal[False] = False


class DecisionRecord(HumanWaitModel):
    id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    request_id: UUID
    plan_content_sha256: Hash
    previous_incident_version: int = Field(ge=1, strict=True)
    incident_version: int = Field(ge=2, strict=True)
    event_cursor: int = Field(ge=1, strict=True)
    outcome: DecisionOutcome
    policy_version: Literal["candidate-approval-decision-v1"]
    decided_by_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=100)
    command_sha256: Hash
    content: DecisionContent
    content_sha256: Hash
    created_at: AwareDatetime


class DecisionView(HumanWaitModel):
    record: DecisionRecord | None
    availability: DecisionAvailability


class DecisionSubmissionResult(HumanWaitModel):
    applied: bool
    decision: DecisionView


class DecisionAuditRecord(HumanWaitModel):
    cursor: int
    authorization_id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    subject_id: str
    command: DecisionCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    decision_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: AwareDatetime


class DecisionAuditPage(HumanWaitModel):
    items: tuple[DecisionAuditRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class DecisionError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
