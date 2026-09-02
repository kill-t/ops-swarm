"""操作执行持久化读取模型。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from opsswarm.approval import ApprovalRequestStatus
from opsswarm.approval.persistence import StoredApprovalRequest
from opsswarm.execution.models import (
    ExecutionStatus,
    OperationFailure,
    OperationResult,
    StrictExecutionModel,
)
from opsswarm.incidents import IncidentSeverity, IncidentStatus
from opsswarm.planning import ActionPlanStatus
from opsswarm.planning.persistence import StoredActionPlan, StoredActionStep


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


class ExecutionCandidateIncident(StrictExecutionModel):
    """执行页面识别事故和提交乐观锁所需的最小快照。"""

    id: UUID
    display_number: int = Field(ge=1)
    service_name: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    severity: IncidentSeverity
    status: IncidentStatus
    version: int = Field(ge=1)
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def normalize_updated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Incident 更新时间必须包含时区")
        return value.astimezone(UTC)


class StoredExecutionCandidate(StrictExecutionModel):
    """当前已批准计划及其仍在运行的幂等操作。"""

    approval_request: StoredApprovalRequest
    incident: ExecutionCandidateIncident
    action_plan: StoredActionPlan
    active_executions: tuple[StoredOperationExecution, ...]

    @model_validator(mode="after")
    def validate_exact_authorization(self) -> Self:
        request = self.approval_request
        if request.status is not ApprovalRequestStatus.APPROVED:
            raise ValueError("待执行投影只能包含 approved 审批")
        if self.incident.status is not IncidentStatus.MITIGATING:
            raise ValueError("待执行计划必须关联 mitigating Incident")
        if self.action_plan.status is not ActionPlanStatus.APPROVED:
            raise ValueError("待执行计划必须处于 approved")
        if request.incident_id != self.incident.id:
            raise ValueError("审批请求与 Incident 不一致")
        if (
            request.action_plan_id != self.action_plan.id
            or request.action_plan_version != self.action_plan.version
        ):
            raise ValueError("审批请求没有绑定投影中的精确计划版本")
        step_ids = {step.id for step in self.action_plan.steps}
        execution_step_ids: set[UUID] = set()
        for execution in self.active_executions:
            if execution.status is not ExecutionStatus.RUNNING:
                raise ValueError("待执行投影只携带 running 操作")
            if (
                execution.incident_id != self.incident.id
                or execution.approval_request_id != request.id
                or execution.action_plan_id != self.action_plan.id
                or execution.action_plan_version != self.action_plan.version
                or execution.action_step_id not in step_ids
            ):
                raise ValueError("运行记录没有绑定当前批准计划")
            if execution.action_step_id in execution_step_ids:
                raise ValueError("一个步骤不能出现多个运行记录")
            execution_step_ids.add(execution.action_step_id)
        return self


@dataclass(frozen=True, slots=True)
class ExecutionCandidatePage:
    """有界待执行队列及数据库中的总待办数。"""

    items: tuple[StoredExecutionCandidate, ...]
    total: int

    def __post_init__(self) -> None:
        if self.total < len(self.items):
            raise ValueError("待执行总数不能小于当前页数量")


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
