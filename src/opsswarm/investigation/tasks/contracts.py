"""专业任务分派、状态和有限结算的领域契约。"""

from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from opsswarm.investigation.models import EvidenceKind


class TaskModel(BaseModel):
    """拒绝未知字段，避免调用方误以为额外参数被采用。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class TaskStatus(StrEnum):
    """业务任务状态，技术重试仍属于同一个任务。"""

    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class RoundStatus(StrEnum):
    """只有全部专业任务终态后才能结束轮次。"""

    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class TaskAttemptStatus(StrEnum):
    """一次专业任务执行尝试的结果。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    LEASE_EXPIRED = "lease_expired"


class TaskAssignment(TaskModel):
    """创建后不可修改的专业任务范围，不向模型授予业务写权限。"""

    task_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    role: EvidenceKind
    goal: str = Field(min_length=1, max_length=1000)
    service_name: str = Field(min_length=1, max_length=100)
    window_start: AwareDatetime
    window_end: AwareDatetime
    max_attempts: int = Field(default=3, ge=1, le=3, strict=True)
    evidence_snapshot_id: str | None = Field(default=None, min_length=1, max_length=100)

    @field_validator("window_start", "window_end")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        """等价时区输入产生相同的分派内容摘要。"""

        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        if not timedelta(0) < self.window_end - self.window_start <= timedelta(days=1):
            raise ValueError("调查时间窗必须为正且不超过一天")
        return self


class RoundAssignment(TaskModel):
    """同一轮的有界分派整体持久化，禁止部分创建。"""

    round_number: int = Field(ge=1, le=3, strict=True)
    goal: str = Field(min_length=1, max_length=1000)
    tasks: Annotated[tuple[TaskAssignment, ...], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def validate_keys(self) -> Self:
        if len({task.task_key for task in self.tasks}) != len(self.tasks):
            raise ValueError("同一轮不能存在重复任务键")
        return self


class TaskOutcome(TaskModel):
    """只保存有限处理摘要或稳定错误码，不接受原始异常文本。"""

    status: Literal["succeeded", "failed"]
    summary: str | None = Field(default=None, min_length=1, max_length=2000)
    error_code: str | None = Field(default=None, pattern=r"^[a-z][a-z0-9_]{0,99}$")
    retryable: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.status == "succeeded":
            if self.summary is None or self.error_code is not None or self.retryable:
                raise ValueError("成功结算必须有摘要，且不能有错误或重试标记")
        elif self.error_code is None or self.summary is not None:
            raise ValueError("失败结算必须有稳定错误码，不能保存成功摘要")
        return self
