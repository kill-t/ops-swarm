"""审批请求持久化读取模型。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opsswarm.approval.models import ApprovalRequestStatus
from opsswarm.incidents import ActorType


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
