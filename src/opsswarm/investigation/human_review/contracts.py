"""显式重评命令、独立任务及审查结果读取契约。"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from opsswarm.investigation.human_prepare.contracts import PreparationAttemptView, PreparationClaim
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel, WaitCommandBase
from opsswarm.investigation.review.contracts import ReviewModelResult

POLICY_VERSION = "human-input-reassessment-v1"


class ReassessCommand(WaitCommandBase):
    action: Literal["reassess"] = "reassess"
    preparation_sha256: Hash


class HumanReviewClaim(PreparationClaim):
    """原工作项不重开，执行身份只属于独立重评尝试。"""


class HumanReviewResultView(HumanWaitModel):
    source_call_id: UUID
    content: ReviewModelResult
    content_sha256: Hash
    created_at: AwareDatetime


class HumanReviewJobView(HumanWaitModel):
    id: UUID
    investigation_run_id: UUID
    preparation_job_id: UUID
    preparation_sha256: Hash
    wait_version: int = Field(ge=2, le=16)
    policy_version: Literal["human-input-reassessment-v1"]
    requested_by_id: str
    authorization_id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    created_at: AwareDatetime
    completed_at: AwareDatetime | None
    last_error_code: str | None
    attempts: tuple[PreparationAttemptView, ...] = Field(max_length=3)
    result: HumanReviewResultView | None
    availability: Literal["pending", "failed", "current", "stale"]


class HumanReviewSubmissionResult(HumanWaitModel):
    applied: bool
    job: HumanReviewJobView


class HumanReviewAuditRecord(HumanWaitModel):
    cursor: int
    authorization_id: UUID
    investigation_run_id: UUID
    preparation_job_id: UUID
    subject_id: str
    command: ReassessCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    job_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: AwareDatetime


class HumanReviewAuditPage(HumanWaitModel):
    items: tuple[HumanReviewAuditRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class HumanReviewError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
