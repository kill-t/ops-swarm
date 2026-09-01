"""订单服务启动时注入的只读运行身份。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.change_records.models import ContainerImageDigest, ImageRepository

MAX_RUNTIME_INVENTORY_DECLARATION_BYTES = 64 * 1024
MAX_RUNTIME_INVENTORY_PROOF_BYTES = 64 * 1024
MAX_RUNTIME_PROOF_PUBLIC_KEYS_BYTES = 4 * 1024
MAX_RUNTIME_PROOF_LIFETIME_SECONDS = 5 * 60
MAX_ROLLOUT_DURATION_SECONDS = 30 * 60
RuntimeInventoryProofErrorCode = Literal[
    "runtime_inventory_proof_not_yet_valid",
    "runtime_inventory_proof_expired",
    "runtime_inventory_proof_invalid",
]


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


class RuntimeArtifactInstance(RuntimeArtifactIdentity):
    """运行清单中的单个实例，不包含主机地址或容器控制信息。"""

    started_at: datetime

    @model_validator(mode="after")
    def normalize_started_at(self) -> Self:
        if self.started_at.tzinfo is None or self.started_at.utcoffset() is None:
            raise ValueError("实例启动时间必须包含时区")
        object.__setattr__(self, "started_at", self.started_at.astimezone(UTC))
        return self


class RuntimeRolloutContext(_StrictModel):
    """由部署控制面注入的滚动发布窗口；稳定状态不得携带发布字段。"""

    phase: Literal["stable", "progressing"] = "stable"
    rollout_id: UUID | None = None
    started_at: datetime | None = None
    deadline_at: datetime | None = None
    target_image_repository: ImageRepository | None = None
    target_image_digest: ContainerImageDigest | None = None

    @model_validator(mode="after")
    def validate_phase_fields(self) -> Self:
        rollout_values = (
            self.rollout_id,
            self.started_at,
            self.deadline_at,
            self.target_image_repository,
            self.target_image_digest,
        )
        if self.phase == "stable":
            if any(value is not None for value in rollout_values):
                raise ValueError("稳定状态不能携带滚动发布字段")
            return self
        if any(value is None for value in rollout_values):
            raise ValueError("滚动发布状态必须完整声明发布身份和时间窗口")
        assert self.started_at is not None
        assert self.deadline_at is not None
        if (
            self.started_at.tzinfo is None
            or self.started_at.utcoffset() is None
            or self.deadline_at.tzinfo is None
            or self.deadline_at.utcoffset() is None
        ):
            raise ValueError("滚动发布时间必须包含时区")
        started_at = self.started_at.astimezone(UTC)
        deadline_at = self.deadline_at.astimezone(UTC)
        if started_at >= deadline_at:
            raise ValueError("滚动发布截止时间必须晚于开始时间")
        if (deadline_at - started_at).total_seconds() > MAX_ROLLOUT_DURATION_SECONDS:
            raise ValueError("滚动发布窗口不能超过 30 分钟")
        object.__setattr__(self, "started_at", started_at)
        object.__setattr__(self, "deadline_at", deadline_at)
        return self


class RuntimeInventoryDeclaration(_StrictModel):
    """部署控制面注入的其他实例和发布窗口，不重复声明当前进程。"""

    additional_instances: Annotated[tuple[RuntimeArtifactInstance, ...], Field(max_length=63)] = ()
    rollout: RuntimeRolloutContext = Field(default_factory=RuntimeRolloutContext)

    @model_validator(mode="after")
    def reject_duplicate_instances(self) -> Self:
        instance_ids = [item.instance_id for item in self.additional_instances]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("运行清单不能包含重复实例 ID")
        return self


class RuntimeInventoryProofSummary(_StrictModel):
    """服务端已经验签的证明摘要，不暴露签名原文或公钥。"""

    proof_id: UUID
    key_id: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$")]
    algorithm: Literal["Ed25519"] = "Ed25519"
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def normalize_timeline(self) -> Self:
        if (
            self.issued_at.tzinfo is None
            or self.issued_at.utcoffset() is None
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
        ):
            raise ValueError("运行证明时间必须包含时区")
        issued_at = self.issued_at.astimezone(UTC)
        expires_at = self.expires_at.astimezone(UTC)
        if issued_at >= expires_at:
            raise ValueError("运行证明过期时间必须晚于签发时间")
        if (expires_at - issued_at).total_seconds() > MAX_RUNTIME_PROOF_LIFETIME_SECONDS:
            raise ValueError("运行证明有效期不能超过 5 分钟")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        return self


class RuntimeInventoryResponse(_StrictModel):
    """部署系统声明的有界实例清单，用于确定性识别滚动发布。"""

    schema_version: Literal[1] = 1
    service_name: Literal["order-service"] = "order-service"
    identity_source: Literal["deployment-environment", "deployment-controller-ed25519"] = (
        "deployment-environment"
    )
    instances: Annotated[tuple[RuntimeArtifactInstance, ...], Field(min_length=1, max_length=64)]
    rollout: RuntimeRolloutContext = Field(default_factory=RuntimeRolloutContext)
    proof: RuntimeInventoryProofSummary | None = None
    reported_at: datetime

    @model_validator(mode="after")
    def validate_inventory_timeline(self) -> Self:
        if self.reported_at.tzinfo is None or self.reported_at.utcoffset() is None:
            raise ValueError("运行清单报告时间必须包含时区")
        reported_at = self.reported_at.astimezone(UTC)
        instance_ids = [item.instance_id for item in self.instances]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("运行清单不能包含重复实例 ID")
        if any(item.started_at > reported_at for item in self.instances):
            raise ValueError("实例启动时间不能晚于清单报告时间")
        if self.rollout.started_at is not None and self.rollout.started_at > reported_at:
            raise ValueError("滚动发布开始时间不能晚于清单报告时间")
        if self.identity_source == "deployment-environment" and self.proof is not None:
            raise ValueError("未签名运行清单不能携带证明摘要")
        if self.identity_source == "deployment-controller-ed25519":
            if self.proof is None:
                raise ValueError("签名运行清单必须携带证明摘要")
            if not self.proof.issued_at <= reported_at < self.proof.expires_at:
                raise ValueError("签名运行清单只能在证明有效期内报告")
        object.__setattr__(self, "reported_at", reported_at)
        return self


class RuntimeInventoryUnavailableResponse(_StrictModel):
    """运行清单没有由部署系统显式启用。"""

    error_code: Literal["runtime_inventory_unavailable"] = "runtime_inventory_unavailable"
    message: str = "运行清单未启用"


class RuntimeInventoryProofUnavailableResponse(_StrictModel):
    """签名运行清单无法通过当前时刻的完整验证。"""

    error_code: RuntimeInventoryProofErrorCode
    message: str


class RuntimeMetadataSettings(BaseSettings):
    """默认关闭；启用后要求完整且精确的启动身份。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_RUNTIME_", extra="ignore")

    metadata_enabled: bool = False
    instance_id: str = ""
    image_repository: str = ""
    image_digest: str = ""
    inventory_declaration: str = Field(
        default="",
        max_length=MAX_RUNTIME_INVENTORY_DECLARATION_BYTES,
    )
    inventory_proof: str = Field(default="", max_length=MAX_RUNTIME_INVENTORY_PROOF_BYTES)
    proof_public_keys: str = Field(default="", max_length=MAX_RUNTIME_PROOF_PUBLIC_KEYS_BYTES)

    @model_validator(mode="after")
    def require_complete_identity_when_enabled(self) -> Self:
        if self.metadata_enabled:
            identity = self.identity()
            assert identity is not None
            proof_enabled = bool(self.inventory_proof.strip() or self.proof_public_keys.strip())
            if proof_enabled:
                if not self.inventory_proof.strip() or not self.proof_public_keys.strip():
                    raise ValueError("签名运行清单必须同时提供证明和公钥环")
                if self.inventory_declaration.strip():
                    raise ValueError("签名证明与未签名运行清单声明不能同时启用")
            else:
                declaration = self.declaration()
                if any(
                    item.instance_id == identity.instance_id
                    for item in declaration.additional_instances
                ):
                    raise ValueError("其他实例不能重复当前进程实例 ID")
        elif any(
            value.strip()
            for value in (
                self.inventory_declaration,
                self.inventory_proof,
                self.proof_public_keys,
            )
        ):
            raise ValueError("运行身份关闭时不能注入实例清单或签名证明")
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

    def declaration(self) -> RuntimeInventoryDeclaration:
        """空声明代表单实例稳定状态；非空 JSON 必须满足精确契约。"""

        if not self.inventory_declaration.strip():
            return RuntimeInventoryDeclaration()
        return RuntimeInventoryDeclaration.model_validate_json(self.inventory_declaration)

    @property
    def proof_enabled(self) -> bool:
        """只有证明和公钥环成对出现时才进入签名模式。"""

        return bool(self.inventory_proof.strip() and self.proof_public_keys.strip())
