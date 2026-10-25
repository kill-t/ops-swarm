"""新核验审批的严格命令、不可变引用与业务审计契约。"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel

POLICY_VERSION: Literal["condition-approval-v1"] = "condition-approval-v1"
ApprovalAvailability = Literal["pending", "current", "superseded", "stale", "expired", "recorded"]
DecisionAction = Literal["approve", "reject", "expire"]
DecisionOutcome = Literal["approved", "rejected", "expired"]
IdempotencyKey = Annotated[
    str, Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$")
]


class RequestCommand(HumanWaitModel):
    verification_id: UUID
    verification_content_sha256: Hash
    expected_incident_version: int = Field(ge=2, strict=True)
    idempotency_key: IdempotencyKey
    reason: str = Field(min_length=1, max_length=500)


class DecisionCommand(HumanWaitModel):
    action: DecisionAction
    request_id: UUID
    request_content_sha256: Hash
    expected_incident_version: int = Field(ge=2, strict=True)
    acknowledge_verification: bool = Field(default=False, strict=True)
    idempotency_key: IdempotencyKey
    reason: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def validate_acknowledgement(self) -> Self:
        if self.acknowledge_verification != (self.action == "approve"):
            raise ValueError("仅批准必须明确确认已阅读完整核验")
        return self


class _ContentReferences(HumanWaitModel):
    verification_id: UUID
    verification_version: int = Field(ge=1, le=8, strict=True)
    verification_content_sha256: Hash
    base_decision_id: UUID
    base_decision_content_sha256: Hash
    plan_id: UUID
    plan_content_sha256: Hash
    conditions_status: Literal["ready_for_review"] = "ready_for_review"
    execution_eligible: Literal[False] = False

    @field_validator("execution_eligible", mode="before")
    @classmethod
    def require_boolean_flag(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("执行资格必须是JSON布尔值")
        return value


class RequestContent(_ContentReferences):
    version: Literal["condition-approval-request-content-v1"] = (
        "condition-approval-request-content-v1"
    )


class RequestRecord(HumanWaitModel):
    id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    verification_id: UUID
    verification_content_sha256: Hash
    verification_version: int = Field(ge=1, le=8, strict=True)
    incident_version: int = Field(ge=2, strict=True)
    policy_version: Literal["condition-approval-v1"]
    status: Literal["pending"] = "pending"
    requested_by_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    idempotency_key: IdempotencyKey
    command_sha256: Hash
    content: RequestContent
    content_sha256: Hash
    created_at: AwareDatetime
    expires_at: AwareDatetime


class RequestView(HumanWaitModel):
    record: RequestRecord | None
    availability: ApprovalAvailability


class RequestResult(HumanWaitModel):
    applied: bool
    request: RequestView


class DecisionContent(_ContentReferences):
    version: Literal["condition-approval-decision-content-v1"] = (
        "condition-approval-decision-content-v1"
    )
    request_id: UUID
    request_content_sha256: Hash
    outcome: DecisionOutcome


class DecisionRecord(HumanWaitModel):
    id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    request_id: UUID
    request_content_sha256: Hash
    verification_id: UUID
    verification_content_sha256: Hash
    verification_version: int = Field(ge=1, le=8, strict=True)
    incident_version: int = Field(ge=2, strict=True)
    policy_version: Literal["condition-approval-v1"]
    outcome: DecisionOutcome
    decided_by_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    idempotency_key: IdempotencyKey
    command_sha256: Hash
    content: DecisionContent
    content_sha256: Hash
    created_at: AwareDatetime


class DecisionView(HumanWaitModel):
    record: DecisionRecord | None
    availability: ApprovalAvailability


class DecisionResult(HumanWaitModel):
    applied: bool
    decision: DecisionView


class ApprovalAuditRecord(HumanWaitModel):
    cursor: int
    stage: Literal["request", "decision"]
    authorization_id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    verification_version: int = Field(ge=1, le=8, strict=True)
    subject_id: str
    command: RequestCommand | DecisionCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    request_id: UUID | None
    decision_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_command_stage(self) -> Self:
        if (self.stage == "request") != isinstance(self.command, RequestCommand):
            raise ValueError("审计阶段必须与命令类型一致")
        if self.outcome == "error":
            valid = (
                self.http_status in {404, 409}
                and self.request_id is None
                and self.decision_id is None
                and self.error_code is not None
                and self.error_message is not None
            )
        else:
            valid = (
                self.http_status == (201 if self.outcome == "applied" else 200)
                and self.request_id is not None
                and (self.decision_id is not None) == (self.stage == "decision")
                and self.error_code is None
                and self.error_message is None
            )
        if not valid:
            raise ValueError("审计结果、HTTP状态与业务引用必须一致")
        return self


class ApprovalAuditPage(HumanWaitModel):
    items: tuple[ApprovalAuditRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class ApprovalError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
