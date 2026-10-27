"""可信回流命令、原预算绑定及不可变请求的有界协议。"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel
from opsswarm.verification.candidate.contracts import UtcTime

POLICY_VERSION: Literal["candidate-reinvestigation-admission-v1"] = (
    "candidate-reinvestigation-admission-v1"
)
OPERATION = "request_candidate_reinvestigation"
ROUTE = "/api/v1/investigations/{run_id}/reinvestigation-admission"


class ReinvestigationCommand(HumanWaitModel):
    action: Literal["request_reinvestigation"] = "request_reinvestigation"
    convergence_sha256: Hash
    expected_incident_version: int = Field(ge=1, le=2147483647, strict=True)
    idempotency_key: str = Field(
        min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    reason: str = Field(min_length=1, max_length=1000)


class ReinvestigationContent(HumanWaitModel):
    id: UUID
    investigation_run_id: UUID
    incident_id: UUID
    root_run_id: UUID
    previous_run_id: UUID
    budget_run_id: UUID
    convergence_content_sha256: Hash
    incident_version: int = Field(ge=1, strict=True)
    policy_version: Literal["candidate-reinvestigation-admission-v1"] = POLICY_VERSION
    max_reflows: Literal[1] = 1
    reflow_number: Literal[1] = 1
    dispatch_status: Literal["not_dispatched"] = "not_dispatched"
    budget: CallBudgetSnapshot
    subject_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    command: ReinvestigationCommand
    command_sha256: Hash
    created_at: UtcTime


class ReinvestigationRecord(ReinvestigationContent):
    content_sha256: Hash


class ReinvestigationView(HumanWaitModel):
    record: ReinvestigationRecord | None
    availability: Literal["pending", "current", "stale", "expired", "blocked"]
    blocking_reason_codes: tuple[str, ...] = Field(max_length=12)


class ReinvestigationSubmission(HumanWaitModel):
    applied: bool
    request: ReinvestigationView


class ReinvestigationAudit(HumanWaitModel):
    cursor: int
    authorization_id: UUID
    investigation_run_id: UUID
    subject_id: str
    command: ReinvestigationCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    request_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: UtcTime


class ReinvestigationAuditPage(HumanWaitModel):
    items: tuple[ReinvestigationAudit, ...] = Field(max_length=100)
    next_cursor: int | None


class ReinvestigationError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
