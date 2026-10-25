"""演练发布的有限目录、严格命令和历史/当前响应。"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

ReleaseVersion = Literal["stable", "2026.01.01-rc1"]
Revision = Annotated[int, Field(strict=True, ge=1, le=2147483647)]
Key = Annotated[str, Field(min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$")]


class ReleaseModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class LabReleaseSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="OPSSWARM_LAB_RELEASE_", extra="ignore")
    enabled: bool = False


class ReleaseCommand(ReleaseModel):
    expected_version: ReleaseVersion
    expected_revision: Revision
    target_version: ReleaseVersion
    idempotency_key: Key


class LegacyFaultCommand(ReleaseModel):
    enabled: bool = Field(strict=True)
    idempotency_key: Key


class ReleaseState(ReleaseModel):
    service_name: Literal["order-service"] = "order-service"
    source: Literal["local-release-simulation-v1"] = "local-release-simulation-v1"
    simulation: Literal[True] = True
    version: ReleaseVersion
    revision: Revision
    command_count: int = Field(strict=True, ge=0, le=2147483647)
    release_500_enabled: bool = Field(strict=True)
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def require_consistent_fault(self) -> Self:
        if self.release_500_enabled != (self.version == "2026.01.01-rc1"):
            raise ValueError("演练故障必须由固定版本决定")
        return self


class ReleaseCommandRecord(ReleaseModel):
    id: UUID
    service_name: Literal["order-service"]
    sequence: Revision
    kind: Literal["cas", "legacy_fault_switch"]
    command: ReleaseCommand | LegacyFaultCommand
    command_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    previous_version: ReleaseVersion
    previous_revision: Revision
    version: ReleaseVersion
    revision: Revision
    created_at: AwareDatetime

    @model_validator(mode="after")
    def require_consistent_transition(self) -> Self:
        if isinstance(self.command, ReleaseCommand):
            valid = (
                self.kind == "cas"
                and self.command.expected_version == self.previous_version
                and self.command.expected_revision == self.previous_revision
            )
            target = self.command.target_version
        else:
            valid = self.kind == "legacy_fault_switch"
            target = "2026.01.01-rc1" if self.command.enabled else "stable"
        if (
            not valid
            or self.version != target
            or self.revision != self.previous_revision + (self.version != self.previous_version)
        ):
            raise ValueError("演练命令必须绑定前态和相邻修订")
        return self


class ReleaseResult(ReleaseModel):
    applied: bool
    command: ReleaseCommandRecord
    current: ReleaseState
    effect_current: bool


class ReleaseError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
