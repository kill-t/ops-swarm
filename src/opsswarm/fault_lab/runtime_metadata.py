"""订单服务启动时注入的只读运行身份。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.change_records.models import ContainerImageDigest, ImageRepository


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeArtifactIdentity(_StrictModel):
    """部署平台在创建当前进程时注入的不可变镜像身份。"""

    instance_id: UUID
    image_repository: ImageRepository
    image_digest: ContainerImageDigest


class RuntimeMetadataResponse(RuntimeArtifactIdentity):
    """只读运行元数据响应，不包含主机或容器控制能力。"""

    schema_version: Literal[1] = 1
    service_name: Literal["order-service"] = "order-service"
    identity_source: Literal["deployment-environment"] = "deployment-environment"
    started_at: datetime
    reported_at: datetime

    @model_validator(mode="after")
    def normalize_timeline(self) -> Self:
        if (
            self.started_at.tzinfo is None
            or self.started_at.utcoffset() is None
            or self.reported_at.tzinfo is None
            or self.reported_at.utcoffset() is None
        ):
            raise ValueError("运行元数据时间必须包含时区")
        started_at = self.started_at.astimezone(UTC)
        reported_at = self.reported_at.astimezone(UTC)
        if started_at > reported_at:
            raise ValueError("进程启动时间不能晚于元数据报告时间")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "reported_at", reported_at)
        return self


class RuntimeMetadataUnavailableResponse(_StrictModel):
    """运行身份未由部署系统显式注入。"""

    error_code: Literal["runtime_metadata_unavailable"] = "runtime_metadata_unavailable"
    message: str = "运行元数据未启用"


class RuntimeMetadataSettings(BaseSettings):
    """默认关闭；启用后要求完整且精确的启动身份。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_RUNTIME_", extra="ignore")

    metadata_enabled: bool = False
    instance_id: str = ""
    image_repository: str = ""
    image_digest: str = ""

    @model_validator(mode="after")
    def require_complete_identity_when_enabled(self) -> Self:
        if self.metadata_enabled:
            self.identity()
        return self

    def identity(self) -> RuntimeArtifactIdentity | None:
        """关闭时明确返回空；启用时返回已校验身份。"""

        if not self.metadata_enabled:
            return None
        return RuntimeArtifactIdentity(
            instance_id=UUID(self.instance_id),
            image_repository=self.image_repository,
            image_digest=self.image_digest,
        )
