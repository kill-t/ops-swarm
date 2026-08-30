"""变更记录模拟器的精确数据契约。"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

CHANGE_SOURCE: Literal["fault-lab-change-simulator"] = "fault-lab-change-simulator"
SUPPORTED_SERVICE: Literal["order-service"] = "order-service"
MAX_CHANGE_RECORDS = 100
ExternalChangeId = Annotated[
    str,
    Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
]
ReleaseVersion = Annotated[
    str,
    Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$"),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class DeploymentChange(_StrictModel):
    """一次已经完成的 order-service 版本发布。"""

    change_type: Literal["deployment"] = "deployment"
    external_change_id: ExternalChangeId
    from_version: ReleaseVersion
    to_version: ReleaseVersion
    completed: Literal[True] = True

    @model_validator(mode="after")
    def require_version_change(self) -> Self:
        if self.from_version == self.to_version:
            raise ValueError("发布前后版本不能相同")
        return self


class ConfigurationChange(_StrictModel):
    """一次已经完成的数据库连接池配置变更。"""

    change_type: Literal["configuration"] = "configuration"
    external_change_id: ExternalChangeId
    key: Literal["database_pool_size"] = "database_pool_size"
    from_value: int = Field(ge=1, le=1000, strict=True)
    to_value: int = Field(ge=1, le=1000, strict=True)
    completed: Literal[True] = True

    @model_validator(mode="after")
    def require_value_change(self) -> Self:
        if self.from_value == self.to_value:
            raise ValueError("配置变更前后值不能相同")
        return self


class DependencyMaintenanceChange(_StrictModel):
    """一次已经完成的固定 Redis 维护重启。"""

    change_type: Literal["dependency_maintenance"] = "dependency_maintenance"
    external_change_id: ExternalChangeId
    dependency: Literal["redis"] = "redis"
    operation: Literal["restart"] = "restart"
    order_release_changed: Literal[False] = False
    completed: Literal[True] = True


ChangeSpec = Annotated[
    DeploymentChange | ConfigurationChange | DependencyMaintenanceChange,
    Field(discriminator="change_type"),
]
CHANGE_SPEC_ADAPTER: TypeAdapter[ChangeSpec] = TypeAdapter(ChangeSpec)


class ChangeRecord(_StrictModel):
    """追加写入 PostgreSQL 后的不可变变更事实。"""

    record_id: UUID
    source: Literal["fault-lab-change-simulator"] = CHANGE_SOURCE
    service_name: Literal["order-service"] = SUPPORTED_SERVICE
    occurred_at: datetime
    recorded_at: datetime
    change: ChangeSpec

    @field_validator("occurred_at", "recorded_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("变更时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def require_timestamp_order(self) -> Self:
        if self.occurred_at > self.recorded_at:
            raise ValueError("变更发生时间不能晚于记录时间")
        return self


class ChangeRecordWriteResult(_StrictModel):
    """幂等写入结果。"""

    record: ChangeRecord
    deduplicated: bool


class ChangeRecordPage(_StrictModel):
    """仓储内部的有界查询页。"""

    items: tuple[ChangeRecord, ...] = Field(max_length=MAX_CHANGE_RECORDS)
    truncated: bool


class ChangeRecordListResponse(_StrictModel):
    """只读 HTTP 查询返回的完整响应。"""

    generated_at: datetime
    items: tuple[ChangeRecord, ...] = Field(max_length=MAX_CHANGE_RECORDS)
    truncated: bool

    @field_validator("generated_at")
    @classmethod
    def normalize_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("查询生成时间必须包含时区")
        return value.astimezone(UTC)


def change_content_hash(change: ChangeSpec) -> str:
    """以规范 JSON 计算幂等内容摘要，不依赖数据库生成字段。"""

    payload = change.model_dump(mode="json")
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
