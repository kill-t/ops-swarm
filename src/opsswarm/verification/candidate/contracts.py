"""候选执行后的固定窗口、探针与不可变恢复结果契约。"""

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

POLICY_VERSION: Literal["candidate-recovery-v1"] = "candidate-recovery-v1"
PROBE_SOURCE: Literal["local-order-probe-v1"] = "local-order-probe-v1"
PROBE_PATH: Literal["/orders/opsswarm-verification-probe"] = "/orders/opsswarm-verification-probe"
PROBE_ORDER_ID: Literal["opsswarm-verification-probe"] = "opsswarm-verification-probe"
MachineCode = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z][a-z0-9_]*$")]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
RecoveryOutcome = Literal[
    "recovered", "not_recovered", "source_failed", "execution_unknown", "not_executed"
]


def _require_time(value: object) -> object:
    if not isinstance(value, (datetime, str)):
        raise ValueError("时间必须为 UTC datetime 或带 UTC 时区的 ISO 字符串")
    return datetime.fromisoformat(value) if isinstance(value, str) else value


def _require_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("时间必须使用 UTC")
    return value


UtcTime = Annotated[AwareDatetime, BeforeValidator(_require_time), AfterValidator(_require_utc)]


class RecoveryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, revalidate_instances="always")


class ProbeObservation(RecoveryModel):
    """只保存业务身份及状态，拒绝正文默认值补齐或状态含义冲突。"""

    source: Literal["local-order-probe-v1"] = PROBE_SOURCE
    path: Literal["/orders/opsswarm-verification-probe"] = PROBE_PATH
    requested_at: UtcTime
    observed_at: UtcTime
    http_status: int = Field(strict=True, ge=200, le=599)
    value: float = Field(strict=True)
    order_id: Literal["opsswarm-verification-probe"] | None

    @field_validator("value", mode="before")
    @classmethod
    def require_numeric_value(cls, value: object) -> object:
        # typing.Literal 不支持浮点字面量，显式校验保持相同的封闭取值集合。
        if type(value) not in (int, float) or value not in (0.0, 1.0):
            raise ValueError("探针值必须为零或一且不能为布尔值")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.observed_at < self.requested_at:
            raise ValueError("观测不能早于请求")
        if self.value == 0.0:
            valid = 200 <= self.http_status < 300 and self.order_id == PROBE_ORDER_ID
        else:
            valid = 500 <= self.http_status <= 599 and self.order_id is None
        if not valid:
            raise ValueError("探针状态、业务身份与结果不一致")
        return self


class RecoveryJobRecord(RecoveryModel):
    id: UUID
    investigation_run_id: UUID
    candidate_job_id: UUID
    verification_version: int = Field(strict=True, ge=1, le=8)
    operation_id: UUID
    execution_result_sha256: Hash
    policy_version: Literal["candidate-recovery-v1"] = POLICY_VERSION
    window_start: UtcTime
    window_end: UtcTime
    status: Literal["queued", "running", "completed"]
    attempt_count: int = Field(strict=True, ge=0, le=3)
    locked_by: str | None = Field(default=None, min_length=1, max_length=100)
    lease_token: UUID | None = None
    locked_until: UtcTime | None = None
    created_at: UtcTime
    updated_at: UtcTime
    completed_at: UtcTime | None = None


class RecoveryAttemptRecord(RecoveryModel):
    id: UUID
    job_id: UUID
    number: int = Field(strict=True, ge=1, le=3)
    lease_token: UUID
    worker_id: str = Field(min_length=1, max_length=100)
    status: Literal["running", "succeeded", "failed", "lease_expired"]
    started_at: UtcTime
    lease_expires_at: UtcTime
    finished_at: UtcTime | None = None


class RecoverySampleRecord(RecoveryModel):
    job_id: UUID
    attempt_id: UUID
    sequence: int = Field(strict=True, ge=1, le=3)
    observation: ProbeObservation | None
    error_code: MachineCode | None
    created_at: UtcTime

    @model_validator(mode="after")
    def require_result(self) -> Self:
        if (self.observation is None) == (self.error_code is None):
            raise ValueError("样本必须携带观测或失败代码之一")
        return self


class RecoveryResultContent(RecoveryModel):
    version: Literal["candidate-recovery-result-v1"] = "candidate-recovery-result-v1"
    execution_job_id: UUID
    execution_result_sha256: Hash
    operation_id: UUID
    outcome: RecoveryOutcome
    attempt_id: UUID | None
    reason_code: MachineCode
    created_at: UtcTime
    parent_advanced: Literal[False] = False

    @field_validator("parent_advanced", mode="before")
    @classmethod
    def require_false(cls, value: object) -> object:
        if value is not False:
            raise ValueError("恢复结果不得推进父运行")
        return value


class RecoveryClaim(RecoveryModel):
    job_id: UUID
    run_id: UUID
    attempt_id: UUID
    number: int = Field(strict=True, ge=1, le=3)
    lease_token: UUID
    lease_expires_at: UtcTime

    @property
    def investigation_run_id(self) -> UUID:
        return self.run_id


class RecoveryView(RecoveryModel):
    record: RecoveryJobRecord | None
    attempts: tuple[RecoveryAttemptRecord, ...] = Field(max_length=3)
    samples: tuple[RecoverySampleRecord, ...] = Field(max_length=9)
    result: RecoveryResultContent | None


class RecoveryError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
