"""确定性恢复验证的状态、样本与判定契约。"""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from opsswarm.planning import VerificationOperator, VerificationRule

SUPPORTED_VERIFICATION_SIGNALS = frozenset(
    {
        "http_5xx_ratio",
        "database_pool_saturation",
        "redis_unavailable_ratio",
    }
)


class VerificationStatus(StrEnum):
    """一次恢复验证运行的持久生命周期。"""

    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"


class VerificationSampleStatus(StrEnum):
    """一个探针样本的确定性判定。"""

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class VerificationWorkItemStatus(StrEnum):
    """恢复验证调度任务的持久生命周期。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"


class StrictVerificationModel(BaseModel):
    """恢复验证不可变值对象的共同约束。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class VerificationObservation(StrictVerificationModel):
    """固定只读探针返回的有限数值。"""

    signal: str = Field(min_length=1, max_length=100)
    value: float = Field(ge=0, le=1, allow_inf_nan=False)
    http_status: int = Field(ge=100, le=599)


class VerificationFailure(StrictVerificationModel):
    """无法取得可信样本时保存的脱敏失败。"""

    error_code: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    message: str = Field(min_length=1, max_length=500)
    http_status: int | None = Field(default=None, ge=100, le=599)


class VerificationWorkItemError(StrictVerificationModel):
    """调度 Worker 自身失败时保存的有限错误摘要。"""

    error_code: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    message: str = Field(min_length=1, max_length=500)


def sample_status(
    rule: VerificationRule,
    observation: VerificationObservation,
) -> VerificationSampleStatus:
    """只判断白名单信号中已冻结的小于阈值规则。"""

    if rule.signal not in SUPPORTED_VERIFICATION_SIGNALS:
        raise ValueError("验证规则使用了不支持的信号")
    if rule.operator is not VerificationOperator.LESS_THAN:
        raise ValueError("验证规则只支持 less_than")
    if observation.signal != rule.signal:
        raise ValueError("探针信号与计划验证规则不一致")
    if observation.value < rule.threshold:
        return VerificationSampleStatus.PASSED
    return VerificationSampleStatus.FAILED
