"""事故收敛的不可变时点决定与只读协议。"""

from typing import Final, Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from opsswarm.incidents.models import IncidentAction, IncidentStatus
from opsswarm.verification.candidate.contracts import Hash, MachineCode, RecoveryModel, UtcTime

POLICY_VERSION: Final = "candidate-incident-convergence-v1"
ROUTE = "/api/v1/investigations/{run_id}/incident-convergence"


ConvergenceOutcome = Literal[
    "recovered",
    "observed_failure",
    "held_uncertain",
    "held_stale",
    "held_incident_changed",
    "held_new_alert",
]


class ConvergenceContent(RecoveryModel):
    policy_version: Literal["candidate-incident-convergence-v1"] = POLICY_VERSION
    investigation_run_id: UUID
    incident_id: UUID
    settlement_content_sha256: Hash
    before_status: IncidentStatus
    before_version: int = Field(strict=True, ge=1)
    after_status: IncidentStatus
    after_version: int = Field(strict=True, ge=1)
    outcome: ConvergenceOutcome
    reason_code: MachineCode
    fresh_until: UtcTime | None
    latest_alert_id: UUID | None
    latest_alert_ingested_at: UtcTime | None
    actions: tuple[IncidentAction, ...] = Field(max_length=2)
    slot_released: bool = Field(strict=True)
    created_at: UtcTime

    @model_validator(mode="after")
    def require_pair(self) -> Self:
        if (self.latest_alert_id is None) != (self.latest_alert_ingested_at is None):
            raise ValueError("告警身份与入库时刻必须同时存在")
        if (
            self.latest_alert_ingested_at is not None
            and self.latest_alert_ingested_at > self.created_at
        ):
            raise ValueError("不能引用未来入库告警")
        if self.after_version != self.before_version + len(self.actions):
            raise ValueError("事故版本必须覆盖每个合法动作")
        return self


class ConvergenceRecord(ConvergenceContent):
    content_sha256: Hash


class ConvergenceView(RecoveryModel):
    record: ConvergenceRecord | None


class ConvergenceError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
