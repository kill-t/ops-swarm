"""审批请求持久化读取模型。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opsswarm.approval.models import ApprovalRequestStatus
from opsswarm.incidents import ActorType, IncidentSeverity, IncidentStatus
from opsswarm.planning.models import ActionPlanStatus
from opsswarm.planning.persistence.records import StoredActionPlan


class StoredApprovalRequest(BaseModel):
    """绑定 Incident 与精确计划版本的只读审批请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    id: UUID
    incident_id: UUID
    action_plan_id: UUID
    action_plan_version: int = Field(ge=1)
    plan_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    status: ApprovalRequestStatus
    requested_by_type: ActorType
    requested_by_id: str = Field(min_length=1, max_length=100)
    request_reason: str = Field(min_length=1, max_length=500)
    expires_at: datetime
    created_at: datetime
    decided_by_type: ActorType | None = None
    decided_by_id: str | None = Field(default=None, min_length=1, max_length=100)
    decision_reason: str | None = Field(default=None, min_length=1, max_length=500)
    decided_at: datetime | None = None

    @field_validator("expires_at", "created_at", "decided_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("审批时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_lifetime(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("审批请求过期时间必须晚于创建时间")
        decision_fields = (
            self.decided_by_type,
            self.decided_by_id,
            self.decision_reason,
            self.decided_at,
        )
        if self.status is ApprovalRequestStatus.PENDING:
            if any(value is not None for value in decision_fields):
                raise ValueError("pending 审批请求不能包含决定审计字段")
            return self
        if self.decided_by_type is None or self.decision_reason is None or self.decided_at is None:
            raise ValueError("审批终态必须包含决定审计信息")
        if self.decided_at < self.created_at:
            raise ValueError("审批决定时间不能早于请求创建时间")
        if self.status in {ApprovalRequestStatus.APPROVED, ApprovalRequestStatus.REJECTED}:
            if self.decided_by_type is not ActorType.HUMAN or self.decided_by_id is None:
                raise ValueError("批准或拒绝必须记录人工操作者")
        elif self.status is ApprovalRequestStatus.EXPIRED and (
            self.decided_by_type is not ActorType.SYSTEM or self.decided_by_id is not None
        ):
            raise ValueError("过期必须记录为无操作者标识的系统决定")
        return self


class PendingApprovalIncident(BaseModel):
    """待审批列表需要的 Incident 最小快照。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

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


class StoredPendingApproval(BaseModel):
    """完整待审批对象，便于工程师复核精确计划。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approval_request: StoredApprovalRequest
    incident: PendingApprovalIncident
    action_plan: StoredActionPlan

    @model_validator(mode="after")
    def validate_exact_binding(self) -> Self:
        request = self.approval_request
        if request.status is not ApprovalRequestStatus.PENDING:
            raise ValueError("待审批投影只能包含 pending 请求")
        if self.incident.status is not IncidentStatus.AWAITING_APPROVAL:
            raise ValueError("待审批请求必须关联 awaiting_approval Incident")
        if self.action_plan.status is not ActionPlanStatus.AWAITING_APPROVAL:
            raise ValueError("待审批请求必须关联 awaiting_approval 计划")
        if request.incident_id != self.incident.id:
            raise ValueError("审批请求与 Incident 不一致")
        if (
            request.action_plan_id != self.action_plan.id
            or request.action_plan_version != self.action_plan.version
        ):
            raise ValueError("审批请求没有绑定投影中的精确计划版本")
        if not self.action_plan.requires_human_approval:
            raise ValueError("待审批计划必须要求人工审批")
        return self


@dataclass(frozen=True, slots=True)
class PendingApprovalPage:
    """有界待审批队列及数据库中的总待办数。"""

    items: tuple[StoredPendingApproval, ...]
    total: int

    def __post_init__(self) -> None:
        if self.total < len(self.items):
            raise ValueError("待审批总数不能小于当前页数量")


@dataclass(frozen=True, slots=True)
class ApprovalRequestCreation:
    """区分首次创建和幂等重复请求。"""

    request: StoredApprovalRequest
    created: bool


@dataclass(frozen=True, slots=True)
class ApprovalDecisionOutcome:
    """区分首次应用人工决定和同方向幂等重试。"""

    request: StoredApprovalRequest
    decision_applied: bool
