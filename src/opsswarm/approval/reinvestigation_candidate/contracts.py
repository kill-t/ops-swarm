"""候选审批请求、只读阶段与可信命令审计契约。"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator

from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel

POLICY_VERSION: Literal["reinvestigation-approval-v1"] = "reinvestigation-approval-v1"
RequestAvailability = Literal["pending", "current", "stale", "expired"]


class ReflowRequestCommand(HumanWaitModel):
    """确认阅读草案的未决条件，不接受客户端补充执行资格。"""

    action: Literal["request_approval"] = "request_approval"
    plan_content_sha256: Hash
    expected_incident_version: int = Field(ge=1, strict=True)
    acknowledge_unresolved_conditions: Literal[True]
    idempotency_key: str = Field(
        min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("acknowledge_unresolved_conditions", mode="before")
    @classmethod
    def require_explicit_acknowledgement(cls, value: object) -> object:
        # Literal[True] 本身接受整数 1，可信命令必须保留原始布尔语义。
        if value is not True:
            raise ValueError("必须明确确认已阅读原前置条件和未解问题")
        return value


class ReflowRequestRecord(HumanWaitModel):
    """不可变请求绑定原草案及唯一事故迁移，不携带审批决定。"""

    id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    plan_id: UUID
    plan_content_sha256: Hash
    basis_event_cursor: int = Field(ge=1, strict=True)
    previous_incident_version: int = Field(ge=1, strict=True)
    incident_version: int = Field(ge=2, strict=True)
    event_cursor: int = Field(ge=1, strict=True)
    status: Literal["pending"] = "pending"
    policy_version: Literal["reinvestigation-approval-v1"]
    requested_by_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=100)
    command_sha256: Hash
    credential_expires_at: AwareDatetime
    created_at: AwareDatetime
    expires_at: AwareDatetime


class ReflowRequestView(HumanWaitModel):
    record: ReflowRequestRecord | None
    availability: RequestAvailability


class ReflowRequestSubmissionResult(HumanWaitModel):
    applied: bool
    request: ReflowRequestView


class ReflowRequestAuditRecord(HumanWaitModel):
    cursor: int
    authorization_id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    subject_id: str
    command: ReflowRequestCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    request_id: UUID | None
    error_code: str | None
    error_message: str | None
    credential_expires_at: AwareDatetime
    created_at: AwareDatetime


class ReflowRequestAuditPage(HumanWaitModel):
    items: tuple[ReflowRequestAuditRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class ReflowRequestError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
