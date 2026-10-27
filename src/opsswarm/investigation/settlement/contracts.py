"""独立收尾事实与只读查询契约，不复用旧根因结果。"""

from typing import Final, Literal
from uuid import UUID

from pydantic import Field

from opsswarm.verification.candidate.contracts import (
    Hash,
    MachineCode,
    RecoveryModel,
    RecoveryOutcome,
    UtcTime,
)

POLICY_VERSION: Final = "candidate-settlement-v1"
ROUTE = "/api/v1/investigations/{run_id}/candidate-settlement"
EVENT_TYPE: Final = "run.candidate_settled"
Disposition = Literal["recovery_verified", "reinvestigation_required", "human_resolution_required"]


class SettlementContent(RecoveryModel):
    id: UUID
    investigation_run_id: UUID
    recovery_job_id: UUID
    candidate_job_id: UUID
    verification_version: int = Field(strict=True, ge=1, le=8)
    operation_id: UUID
    execution_result_sha256: Hash
    recovery_result_sha256: Hash
    handoff_id: UUID
    previous_run_status: Literal["await_candidate", "await_human"]
    previous_run_updated_at: UtcTime
    incident_version: int = Field(strict=True, ge=2)
    outcome: RecoveryOutcome
    disposition: Disposition
    reason_code: MachineCode
    event_cursor: int = Field(strict=True, ge=1)
    policy_version: Literal["candidate-settlement-v1"] = POLICY_VERSION
    created_at: UtcTime


class SettlementRecord(SettlementContent):
    content_sha256: Hash


class SettlementView(RecoveryModel):
    record: SettlementRecord | None


class SettlementError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
