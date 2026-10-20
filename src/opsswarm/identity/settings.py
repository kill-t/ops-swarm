"""只从环境加载本地操作者凭据，拒绝不完整的认证配置。"""

from datetime import UTC, datetime
from typing import Literal, Self

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.identity.models import OperatorRole


class OperatorCredential(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    credential_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    subject_id: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._:-]{0,99}$")
    role: OperatorRole
    secret_sha256: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False, exclude=True)
    expires_at: AwareDatetime

    @field_validator("expires_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class OperatorAuthSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="OPSSWARM_OPERATOR_AUTH_",
        extra="ignore",
        frozen=True,
        hide_input_in_errors=True,
    )

    mode: Literal["legacy_local", "bearer"] = "legacy_local"
    credentials: tuple[OperatorCredential, ...] = Field(default=(), max_length=32, repr=False)

    @model_validator(mode="after")
    def validate_credentials(self) -> Self:
        if self.mode == "bearer" and not self.credentials:
            raise ValueError("Bearer 认证必须配置操作者凭据")
        ids = [item.credential_id for item in self.credentials]
        hashes = [item.secret_sha256 for item in self.credentials]
        if len(set(ids)) != len(ids) or len(set(hashes)) != len(hashes):
            raise ValueError("操作者凭据身份或摘要重复")
        roles: dict[str, OperatorRole] = {}
        for item in self.credentials:
            if item.subject_id in roles and roles[item.subject_id] != item.role:
                raise ValueError("同一主体的多个凭据必须具有相同角色")
            roles[item.subject_id] = item.role
        return self
