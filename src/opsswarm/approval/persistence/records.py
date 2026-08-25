"""审批请求持久化读取模型。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opsswarm.approval.models import ApprovalRequestStatus
from opsswarm.incidents import ActorType


class StoredApprovalRequest(BaseModel):
    """绑定 Incident 与精确计划版本的只读审批请求。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

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

    @model_validator(mode="after")
    def validate_lifetime(self) -> Self:
        if self.expires_at <= self.created_at:
            raise ValueError("审批请求过期时间必须晚于创建时间")
        return self


@dataclass(frozen=True, slots=True)
class ApprovalRequestCreation:
    """区分首次创建和幂等重复请求。"""

    request: StoredApprovalRequest
    created: bool
