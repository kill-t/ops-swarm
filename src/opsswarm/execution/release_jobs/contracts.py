"""可信执行命令、固定窗口任务和不可变结果契约。"""

from datetime import datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from opsswarm.execution.release_jobs.client import DispatchObservation
from opsswarm.fault_lab.release.contracts import Key
from opsswarm.fault_lab.release.dispatch_contracts import DispatchCommand
from opsswarm.fault_lab.release.receipts import receipt_time
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel

POLICY_VERSION = "candidate-release-execution-v1"
ROUTE = (
    "/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}"
    "/execution-conditions/versions/{version}/release-execution-request/execution-job"
)
OPERATION = "create_candidate_release_execution_job"


class ExecutionCommand(HumanWaitModel):
    request_id: UUID
    request_content_sha256: Hash
    expected_incident_version: int = Field(strict=True, ge=2)
    idempotency_key: Key
    reason: str = Field(min_length=1, max_length=500)


class ExecutionClaim(HumanWaitModel):
    job_id: UUID
    investigation_run_id: UUID
    attempt_id: UUID
    number: int = Field(strict=True, ge=1, le=3)
    lease_token: UUID
    lease_expires_at: AwareDatetime


class ExecutionResultContent(HumanWaitModel):
    version: Literal["candidate-release-execution-result-v1"] = (
        "candidate-release-execution-result-v1"
    )
    request_id: UUID
    request_content_sha256: Hash
    operation_id: UUID
    dispatch_sha256: Hash
    outcome: Literal["applied_current", "applied_superseded", "not_applied", "unknown", "blocked"]
    observation: DispatchObservation | None
    reason_code: str = Field(min_length=1, max_length=100)
    created_at: AwareDatetime
    parent_advanced: Literal[False] = False

    @field_validator("parent_advanced", mode="before")
    @classmethod
    def require_false_boolean(cls, value: object) -> object:
        if value is not False:
            raise ValueError("执行结果不能声称推进父运行")
        return value

    @field_validator("created_at", mode="before")
    @classmethod
    def require_time(cls, value: object) -> datetime:
        return receipt_time(value)


class ExecutionJobRecord(HumanWaitModel):
    id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    verification_version: int = Field(strict=True, ge=1, le=8)
    request_id: UUID
    request_content_sha256: Hash
    operation_id: UUID
    policy_version: Literal["candidate-release-execution-v1"]
    requested_by_id: str
    credential_id: str
    credential_binding_sha256: Hash
    credential_expires_at: AwareDatetime
    authorization_id: UUID
    idempotency_key: Key
    command_sha256: Hash
    dispatch_command: DispatchCommand
    dispatch_sha256: Hash
    status: Literal["queued", "running", "succeeded", "failed", "unknown"]
    phase: Literal["prepared", "dispatched"]
    attempt_count: int = Field(strict=True, ge=0, le=3)
    locked_by: str | None
    lease_token: UUID | None
    locked_until: AwareDatetime | None
    dispatched_at: AwareDatetime | None
    dispatch_attempt_id: UUID | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None
    last_error_code: str | None


class ExecutionAttemptRecord(HumanWaitModel):
    id: UUID
    job_id: UUID
    number: int = Field(strict=True, ge=1, le=3)
    lease_token: UUID
    worker_id: str
    status: Literal["running", "succeeded", "failed", "lease_expired"]
    started_at: AwareDatetime
    lease_expires_at: AwareDatetime
    finished_at: AwareDatetime | None
    error_code: str | None


class ExecutionJobView(HumanWaitModel):
    record: ExecutionJobRecord | None
    attempts: tuple[ExecutionAttemptRecord, ...] = Field(max_length=3)
    result: ExecutionResultContent | None


class ExecutionSubmission(HumanWaitModel):
    applied: bool
    job: ExecutionJobView


class ExecutionAuditRecord(HumanWaitModel):
    cursor: int
    authorization_id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    verification_version: int = Field(strict=True, ge=1, le=8)
    subject_id: str
    command: ExecutionCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    execution_job_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def require_outcome(self) -> Self:
        if self.outcome == "error":
            valid = (
                self.http_status in {404, 409}
                and self.execution_job_id is None
                and self.error_code is not None
                and self.error_message is not None
            )
        else:
            valid = (
                self.http_status == (201 if self.outcome == "applied" else 200)
                and self.execution_job_id is not None
                and self.error_code is None
                and self.error_message is None
            )
        if not valid:
            raise ValueError("执行任务审计状态与引用不一致")
        return self


class ExecutionAuditPage(HumanWaitModel):
    items: tuple[ExecutionAuditRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class ExecutionJobError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
