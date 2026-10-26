"""候选发布执行准备的可信命令、只读观测与不可派发事实。"""

import re
from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from opsswarm.execution.conditions.contracts import OBSERVATION_TIME_PATTERN
from opsswarm.fault_lab.release.contracts import ReleaseCommand, ReleaseState
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel
from opsswarm.planning.models import ActionStep

POLICY_VERSION: Literal["candidate-release-request-v1"] = "candidate-release-request-v1"
ReleaseRequestAvailability = Literal["pending", "current", "superseded", "stale", "expired"]


class ReleaseRequestCommand(HumanWaitModel):
    approval_decision_id: UUID
    approval_content_sha256: Hash
    expected_incident_version: int = Field(ge=2, strict=True)
    idempotency_key: str = Field(
        min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    reason: str = Field(min_length=1, max_length=500)


def _require_timestamp(value: object) -> object:
    if not isinstance(value, (datetime, str)):
        raise ValueError("观测时间必须是带时区时间或ISO8601字符串")
    if isinstance(value, str) and re.fullmatch(OBSERVATION_TIME_PATTERN, value) is None:
        raise ValueError("观测时间须使用带时区ISO8601格式，精度最多六位小数")
    return value


class ReleaseObservation(HumanWaitModel):
    state: ReleaseState
    observed_at: AwareDatetime
    source_path: Literal["/__admin/simulated-release"] = "/__admin/simulated-release"

    @field_validator("state", mode="before")
    @classmethod
    def require_complete_simulation(cls, value: object) -> object:
        raw = value.model_dump() if isinstance(value, ReleaseState) else value
        expected = {
            "service_name",
            "source",
            "simulation",
            "version",
            "revision",
            "command_count",
            "release_500_enabled",
            "updated_at",
        }
        if not isinstance(raw, dict) or set(raw) != expected:
            raise ValueError("观测必须携带完整发布演练状态，不补默认来源身份")
        if type(raw["simulation"]) is not bool or raw["simulation"] is not True:
            raise ValueError("观测必须明确标识真实布尔simulation=true")
        _require_timestamp(raw["updated_at"])
        # 已构造DTO也重新走字段校验，不能用未验证的model_copy越过来源边界。
        return raw

    @field_validator("observed_at", mode="before")
    @classmethod
    def require_observed_at(cls, value: object) -> object:
        return _require_timestamp(value)

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        try:
            return value.astimezone(UTC)
        except OverflowError as error:
            raise ValueError("观测时间归一UTC后越界") from error

    @field_validator("state")
    @classmethod
    def normalize_state_time(cls, value: ReleaseState) -> ReleaseState:
        try:
            return value.model_copy(update={"updated_at": value.updated_at.astimezone(UTC)})
        except OverflowError as error:
            raise ValueError("发布状态时间归一UTC后越界") from error

    @model_validator(mode="after")
    def validate_source_time(self) -> Self:
        if self.state.revision > self.state.command_count + 1:
            raise ValueError("演练修订不能超过初态加成功命令数")
        try:
            if self.state.updated_at.astimezone(UTC) > self.observed_at:
                raise ValueError("观测不能包含采集时刻之后的状态")
        except OverflowError as error:
            raise ValueError("发布状态时间归一UTC后越界") from error
        return self


class ReleaseRequestContent(HumanWaitModel):
    version: Literal["candidate-release-execution-request-content-v1"] = (
        "candidate-release-execution-request-content-v1"
    )
    approval_decision_id: UUID
    approval_content_sha256: Hash
    verification_id: UUID
    verification_version: int = Field(ge=1, le=8, strict=True)
    verification_content_sha256: Hash
    plan_id: UUID
    plan_content_sha256: Hash
    operation_id: UUID
    step: ActionStep
    observation: ReleaseObservation
    cas_command: ReleaseCommand
    dispatch_eligible: Literal[False] = False

    @field_validator("dispatch_eligible", mode="before")
    @classmethod
    def require_dispatch_flag(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("派发资格必须是JSON布尔值")
        return value


class ReleaseRequestRecord(HumanWaitModel):
    id: UUID
    operation_id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    approval_decision_id: UUID
    approval_content_sha256: Hash
    verification_version: int = Field(ge=1, le=8, strict=True)
    incident_version: int = Field(ge=2, strict=True)
    policy_version: Literal["candidate-release-request-v1"]
    status: Literal["prepared"] = "prepared"
    requested_by_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=100)
    command_sha256: Hash
    content: ReleaseRequestContent
    content_sha256: Hash
    created_at: AwareDatetime
    expires_at: AwareDatetime


class ReleaseRequestView(HumanWaitModel):
    record: ReleaseRequestRecord | None
    availability: ReleaseRequestAvailability
    requires_dispatch_recheck: Literal[True] = True

    @field_validator("requires_dispatch_recheck", mode="before")
    @classmethod
    def require_recheck_flag(cls, value: object) -> object:
        if type(value) is not bool:
            raise ValueError("派发复查标志必须是JSON布尔值")
        return value


class ReleaseRequestResult(HumanWaitModel):
    applied: bool
    request: ReleaseRequestView


class ReleaseRequestAuditRecord(HumanWaitModel):
    cursor: int
    authorization_id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    verification_version: int = Field(ge=1, le=8, strict=True)
    subject_id: str
    command: ReleaseRequestCommand
    command_sha256: Hash
    outcome: Literal["applied", "replayed", "error"]
    http_status: Literal[200, 201, 404, 409]
    request_id: UUID | None
    error_code: str | None
    error_message: str | None
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_outcome_shape(self) -> Self:
        if self.outcome == "error":
            valid = (
                self.http_status in {404, 409}
                and self.request_id is None
                and self.error_code is not None
                and self.error_message is not None
            )
        else:
            valid = (
                self.http_status == (201 if self.outcome == "applied" else 200)
                and self.request_id is not None
                and self.error_code is None
                and self.error_message is None
            )
        if not valid:
            raise ValueError("准备审计结果、HTTP状态与业务引用不一致")
        return self


class ReleaseRequestAuditPage(HumanWaitModel):
    items: tuple[ReleaseRequestAuditRecord, ...] = Field(max_length=100)
    next_cursor: int | None


class ReleaseRequestError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
