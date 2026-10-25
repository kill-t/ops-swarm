"""候选执行前置检查的严格命令与不可变事实契约。"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from opsswarm.investigation.candidate.output import CandidateItem
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel
from opsswarm.planning.models import ActionStep, ActionType

POLICY_VERSION: Literal["candidate-execution-preflight-v1"] = "candidate-execution-preflight-v1"
ObservationPath = Literal[
    "/__admin/faults/release-500",
    "/__admin/configuration/database-pool",
    "/__admin/dependencies/redis",
]
MachineCode = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")]
OBSERVATION_PATHS: dict[ActionType, ObservationPath] = {
    ActionType.ROLLBACK_RELEASE: "/__admin/faults/release-500",
    ActionType.ROLLBACK_CONFIGURATION: "/__admin/configuration/database-pool",
    ActionType.RESTORE_DEPENDENCY: "/__admin/dependencies/redis",
}


class PreflightCommand(HumanWaitModel):
    """只选择精确批准，不接受客户端声明的当前参数或满足条件。"""

    decision_id: UUID
    decision_content_sha256: Hash
    expected_incident_version: int = Field(ge=2, strict=True)
    idempotency_key: str = Field(
        min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    reason: str = Field(min_length=1, max_length=500)


class RuntimeObservation(HumanWaitModel):
    """一次固定只读来源的采集事实，失败时不得夹带旧参数。"""

    action_type: ActionType
    observed_at: AwareDatetime
    source_path: ObservationPath
    current_value: int | None = Field(default=None, strict=True, ge=1, le=1000)
    available: bool | None = Field(default=None, strict=True)
    release_enabled: bool | None = Field(default=None, strict=True)
    error_code: MachineCode | None = None

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.source_path != OBSERVATION_PATHS[self.action_type]:
            raise ValueError("观测动作必须绑定固定只读路径")
        values = (self.current_value, self.available, self.release_enabled)
        if self.error_code is not None:
            if any(value is not None for value in values):
                raise ValueError("失败观测不能携带运行参数")
        else:
            expected = {
                ActionType.ROLLBACK_CONFIGURATION: 0,
                ActionType.RESTORE_DEPENDENCY: 1,
                ActionType.ROLLBACK_RELEASE: 2,
            }[self.action_type]
            if any(
                (value is not None) != (index == expected) for index, value in enumerate(values)
            ):
                raise ValueError("成功观测只能携带动作对应的唯一运行参数")
        return self


class PreflightContent(HumanWaitModel):
    """保留原条件及明确阻断，当前策略永不产生执行资格。"""

    version: Literal["candidate-execution-preflight-content-v1"] = (
        "candidate-execution-preflight-content-v1"
    )
    decision_id: UUID
    decision_content_sha256: Hash
    plan_content_sha256: Hash
    step: ActionStep
    observation: RuntimeObservation
    preconditions: tuple[CandidateItem, ...] = Field(min_length=1, max_length=10)
    unresolved_questions: tuple[CandidateItem, ...] = Field(max_length=10)
    blockers: tuple[MachineCode, ...] = Field(min_length=1, max_length=20)
    status: Literal["blocked"] = "blocked"
    execution_eligible: Literal[False] = False


class PreflightRecord(HumanWaitModel):
    cursor: int = Field(ge=1, strict=True)
    id: UUID
    operation_id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    decision_id: UUID
    decision_content_sha256: Hash
    incident_version: int = Field(ge=2, strict=True)
    policy_version: Literal["candidate-execution-preflight-v1"]
    requested_by_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=100)
    command_sha256: Hash
    content: PreflightContent
    content_sha256: Hash
    created_at: AwareDatetime
    expires_at: AwareDatetime


class PreflightPage(HumanWaitModel):
    items: tuple[PreflightRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class PreflightResult(HumanWaitModel):
    applied: bool
    record: PreflightRecord


class PreflightAuditRecord(HumanWaitModel):
    cursor: int
    authorization_id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    subject_id: str
    command: PreflightCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    preflight_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: AwareDatetime


class PreflightAuditPage(HumanWaitModel):
    items: tuple[PreflightAuditRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class PreflightError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
