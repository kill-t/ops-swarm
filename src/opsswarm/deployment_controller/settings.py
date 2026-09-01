"""部署控制器启动配置。"""

from __future__ import annotations

from typing import Annotated, Self
from uuid import UUID

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.fault_lab.runtime_metadata import (
    MAX_RUNTIME_INVENTORY_DECLARATION_BYTES,
    RuntimeArtifactIdentity,
    RuntimeInventoryDeclaration,
)
from opsswarm.fault_lab.runtime_proof import decode_ed25519_private_key


class DeploymentControllerSettings(BaseSettings):
    """只有该独立进程持有签名私钥。"""

    model_config = SettingsConfigDict(
        env_prefix="OPSSWARM_DEPLOYMENT_CONTROLLER_",
        extra="ignore",
    )

    private_key: SecretStr = SecretStr("")
    key_id: Annotated[
        str,
        Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
    ] = "local-controller-1"
    instance_id: str = ""
    image_repository: str = ""
    image_digest: str = ""
    inventory_declaration: str = Field(
        default="",
        max_length=MAX_RUNTIME_INVENTORY_DECLARATION_BYTES,
    )
    proof_lifetime_seconds: int = Field(default=120, ge=30, le=300)

    @model_validator(mode="after")
    def validate_signing_material_and_inventory(self) -> Self:
        private_key = self.private_key.get_secret_value()
        if not private_key:
            raise ValueError("部署控制器必须配置 Ed25519 私钥")
        decode_ed25519_private_key(private_key)
        identity = self.identity()
        declaration = self.declaration()
        if any(
            item.instance_id == identity.instance_id for item in declaration.additional_instances
        ):
            raise ValueError("其他实例不能重复当前进程实例 ID")
        return self

    def identity(self) -> RuntimeArtifactIdentity:
        """返回受签名保护的当前订单服务身份。"""

        return RuntimeArtifactIdentity(
            instance_id=UUID(self.instance_id),
            image_repository=self.image_repository,
            image_digest=self.image_digest,
        )

    def declaration(self) -> RuntimeInventoryDeclaration:
        """解析严格的其他实例和发布窗口。"""

        if not self.inventory_declaration.strip():
            return RuntimeInventoryDeclaration()
        return RuntimeInventoryDeclaration.model_validate_json(self.inventory_declaration)

    def load_private_key(self) -> Ed25519PrivateKey:
        """私钥只在控制器启动时解析，不对外序列化。"""

        return decode_ed25519_private_key(self.private_key.get_secret_value())
