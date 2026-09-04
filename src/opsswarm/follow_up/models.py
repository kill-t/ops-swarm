"""复盘改进行动项的确定性领域模型。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opsswarm.incidents import IncidentSeverity


class ReportActionItemStatus(StrEnum):
    """改进行动项从待完成到审计终态的生命周期。"""

    OPEN = "open"
    COMPLETED = "completed"


class ReportActionItemEventType(StrEnum):
    """追加式行动项事件类型。"""

    CREATED = "report_action_item.created"
    COMPLETED = "report_action_item.completed"


class StrictFollowUpModel(BaseModel):
    """行动项领域对象拒绝额外字段并保持不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ReportActionItem(StrictFollowUpModel):
    """一个归属于冻结报告的结构化改进行动项。"""

    id: UUID
    report_id: UUID
    idempotency_key: str = Field(
        min_length=8,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1000)
    owner_id: str = Field(min_length=1, max_length=100)
    status: ReportActionItemStatus
    version: int = Field(ge=1)
    due_at: datetime
    created_by_id: str = Field(min_length=1, max_length=100)
    created_at: datetime
    completed_by_id: str | None = Field(default=None, min_length=1, max_length=100)
    completion_note: str | None = Field(default=None, min_length=1, max_length=500)
    completed_at: datetime | None = None

    @field_validator("due_at", "created_at", "completed_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("行动项时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_lifecycle(self) -> Self:
        if self.due_at <= self.created_at:
            raise ValueError("行动项截止时间必须晚于创建时间")
        completion_fields = (
            self.completed_by_id,
            self.completion_note,
            self.completed_at,
        )
        if self.status is ReportActionItemStatus.OPEN and any(
            value is not None for value in completion_fields
        ):
            raise ValueError("open 行动项不能提前保存完成审计")
        if self.status is ReportActionItemStatus.OPEN and self.version != 1:
            raise ValueError("open 行动项版本必须为 1")
        if self.status is ReportActionItemStatus.COMPLETED and any(
            value is None for value in completion_fields
        ):
            raise ValueError("completed 行动项必须保存完整完成审计")
        if self.status is ReportActionItemStatus.COMPLETED and self.version != 2:
            raise ValueError("completed 行动项版本必须为 2")
        if self.completed_at is not None and self.completed_at < self.created_at:
            raise ValueError("完成时间不能早于创建时间")
        return self


class ReportActionItemEvent(StrictFollowUpModel):
    """一次不能覆盖的行动项状态事实。"""

    cursor: int = Field(ge=1)
    item_id: UUID
    event_type: ReportActionItemEventType
    actor_id: str = Field(min_length=1, max_length=100)
    from_status: ReportActionItemStatus | None
    to_status: ReportActionItemStatus
    reason: str = Field(min_length=1, max_length=500)
    item_version: int = Field(ge=1)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("行动项事件时间必须包含时区")
        return value.astimezone(UTC)


class ReportActionItemCreation(StrictFollowUpModel):
    """区分首次创建和幂等读取。"""

    created: bool
    item: ReportActionItem


class ReportActionItemCompletion(StrictFollowUpModel):
    """区分首次完成和相同完成命令重试。"""

    completion_applied: bool
    item: ReportActionItem


class ReportActionItemPage(StrictFollowUpModel):
    """一个报告下有界的改进行动项列表。"""

    items: tuple[ReportActionItem, ...]
    total: int = Field(ge=0)


class ReportActionItemGovernanceEntry(StrictFollowUpModel):
    """跨事故治理列表中的行动项及其事故上下文。"""

    action_item: ReportActionItem
    incident_id: UUID
    incident_display_number: int = Field(ge=1)
    incident_service_name: str = Field(min_length=1, max_length=100)
    incident_title: str = Field(min_length=1, max_length=200)
    incident_severity: IncidentSeverity
    overdue: bool

    @model_validator(mode="after")
    def require_open_item(self) -> Self:
        if self.action_item.status is not ReportActionItemStatus.OPEN:
            raise ValueError("治理待办只能包含 open 行动项")
        return self


class ReportActionItemGovernancePage(StrictFollowUpModel):
    """带服务器观察时间和筛选回显的跨事故待办页。"""

    items: tuple[ReportActionItemGovernanceEntry, ...]
    total: int = Field(ge=0)
    as_of: datetime
    owner_id: str | None = Field(default=None, min_length=1, max_length=100)
    overdue_only: bool

    @field_validator("as_of")
    @classmethod
    def normalize_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("治理列表观察时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_projection(self) -> Self:
        if self.total < len(self.items):
            raise ValueError("治理列表总数不能小于当前返回数量")
        for entry in self.items:
            expected_overdue = entry.action_item.due_at < self.as_of
            if entry.overdue is not expected_overdue:
                raise ValueError("行动项逾期标识与服务器观察时间不一致")
            if self.owner_id is not None and entry.action_item.owner_id != self.owner_id:
                raise ValueError("治理列表包含其他负责人行动项")
            if self.overdue_only and not entry.overdue:
                raise ValueError("仅逾期列表包含未逾期行动项")
        return self
