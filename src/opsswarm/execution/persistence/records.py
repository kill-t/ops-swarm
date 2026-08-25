"""操作执行持久化读取模型。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from opsswarm.execution.models import (
    ExecutionStatus,
    OperationFailure,
    OperationResult,
    StrictExecutionModel,
)
from opsswarm.planning.persistence import StoredActionStep


class StoredOperationExecution(StrictExecutionModel):
    """一次与批准计划步骤绑定的可恢复执行记录。"""

    id: UUID
    idempotency_key: str = Field(
        min_length=8,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    incident_id: UUID
    approval_request_id: UUID
    action_plan_id: UUID
    action_plan_version: int = Field(ge=1)
    action_step_id: UUID
    status: ExecutionStatus
    executor_name: str
    incident_version_before: int = Field(ge=1)
    attempt_count: int = Field(ge=1)
    lease_token: UUID | None = None
    locked_until: datetime | None = None
    result: OperationResult | None = None
    failure: OperationFailure | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None

    @field_validator("locked_until", "created_at", "updated_at", "completed_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("执行时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.updated_at < self.created_at:
            raise ValueError("执行更新时间不能早于创建时间")
        if self.status is ExecutionStatus.RUNNING:
            if self.lease_token is None or self.locked_until is None:
                raise ValueError("running 执行必须持有租约")
            if self.result is not None or self.failure is not None or self.completed_at is not None:
                raise ValueError("running 执行不能提前包含终态结果")
            return self
        if (
            self.lease_token is not None
            or self.locked_until is not None
            or self.completed_at is None
        ):
            raise ValueError("终态执行必须清理租约并记录完成时间")
        if self.completed_at < self.created_at:
            raise ValueError("执行完成时间不能早于创建时间")
        if self.status is ExecutionStatus.SUCCEEDED:
            if self.result is None or self.failure is not None:
                raise ValueError("succeeded 执行必须且只能包含成功结果")
        elif self.result is not None or self.failure is None:
            raise ValueError("failed 执行必须且只能包含失败结果")
        return self


@dataclass(frozen=True, slots=True)
class ClaimedOperationExecution:
    """领取结果同时携带适配器需要的受校验步骤。"""

    execution: StoredOperationExecution
    step: StoredActionStep
    claimed: bool
    created: bool


@dataclass(frozen=True, slots=True)
class OperationExecutionOutcome:
    """控制面返回的幂等执行结果。"""

    execution: StoredOperationExecution
    created: bool
    execution_performed: bool
