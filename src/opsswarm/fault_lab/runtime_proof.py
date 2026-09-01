"""部署控制器签发、订单服务验签的运行清单证明。"""

from __future__ import annotations

import base64
import binascii
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Literal, Self
from uuid import UUID

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from opsswarm.fault_lab.runtime_metadata import (
    MAX_RUNTIME_PROOF_LIFETIME_SECONDS,
    RuntimeArtifactIdentity,
    RuntimeArtifactInstance,
    RuntimeInventoryProofErrorCode,
    RuntimeInventoryProofSummary,
    RuntimeRolloutContext,
)

ProofKeyId = Annotated[
    str,
    Field(min_length=1, max_length=64, pattern=r"^[a-z0-9][a-z0-9._-]*$"),
]
EncodedEd25519PublicKey = Annotated[
    str,
    Field(min_length=43, max_length=43, pattern=r"^[A-Za-z0-9_-]{43}$"),
]
EncodedEd25519Signature = Annotated[
    str,
    Field(min_length=86, max_length=86, pattern=r"^[A-Za-z0-9_-]{86}$"),
]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeInventoryProofPayload(_StrictModel):
    """Ed25519 签名覆盖的完整、短时运行清单快照。"""

    schema_version: Literal[1] = 1
    proof_id: UUID
    audience: Literal["opsswarm/order-service/runtime-inventory"] = (
        "opsswarm/order-service/runtime-inventory"
    )
    service_name: Literal["order-service"] = "order-service"
    issued_at: datetime
    expires_at: datetime
    instances: Annotated[tuple[RuntimeArtifactInstance, ...], Field(min_length=1, max_length=64)]
    rollout: RuntimeRolloutContext = Field(default_factory=RuntimeRolloutContext)

    @model_validator(mode="after")
    def validate_snapshot(self) -> Self:
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
        instance_ids = [item.instance_id for item in self.instances]
        if len(instance_ids) != len(set(instance_ids)):
            raise ValueError("运行证明不能包含重复实例 ID")
        if any(item.started_at > issued_at for item in self.instances):
            raise ValueError("实例启动时间不能晚于证明签发时间")
        if self.rollout.started_at is not None and self.rollout.started_at > issued_at:
            raise ValueError("滚动发布开始时间不能晚于证明签发时间")
        object.__setattr__(self, "issued_at", issued_at)
        object.__setattr__(self, "expires_at", expires_at)
        return self


class RuntimeInventoryProofEnvelope(_StrictModel):
    """带密钥编号的签名信封，便于部署时平滑轮换公钥。"""

    schema_version: Literal[1] = 1
    key_id: ProofKeyId
    algorithm: Literal["Ed25519"] = "Ed25519"
    payload: RuntimeInventoryProofPayload
    signature: EncodedEd25519Signature


class RuntimeProofPublicKeyRing(_StrictModel):
    """订单服务持有的有界只读公钥集合，不包含任何私钥。"""

    schema_version: Literal[1] = 1
    keys: Annotated[dict[ProofKeyId, EncodedEd25519PublicKey], Field(min_length=1, max_length=4)]

    @model_validator(mode="after")
    def validate_public_keys(self) -> Self:
        for value in self.keys.values():
            _decode_base64url(value, expected_length=32, field_name="Ed25519 公钥")
        return self


class RuntimeInventoryProofVerificationError(ValueError):
    """不向调用方泄露密钥材料的稳定验签错误。"""

    def __init__(self, error_code: RuntimeInventoryProofErrorCode, message: str) -> None:
        super().__init__(message)
        self.error_code = error_code
        self.message = message


@dataclass(frozen=True)
class VerifiedRuntimeInventoryProof:
    """已经完成签名、时效和当前进程绑定校验的内部结果。"""

    envelope: RuntimeInventoryProofEnvelope
    summary: RuntimeInventoryProofSummary

    @property
    def instances(self) -> tuple[RuntimeArtifactInstance, ...]:
        return self.envelope.payload.instances

    @property
    def rollout(self) -> RuntimeRolloutContext:
        return self.envelope.payload.rollout


def _encode_base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode_base64url(value: str, *, expected_length: int, field_name: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{field_name}不是合法的无填充 Base64URL") from exc
    if len(decoded) != expected_length:
        raise ValueError(f"{field_name}长度不正确")
    return decoded


def canonical_runtime_inventory_payload(payload: RuntimeInventoryProofPayload) -> bytes:
    """使用稳定字段顺序和紧凑分隔符生成唯一签名字节。"""

    return json.dumps(
        payload.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def encode_ed25519_public_key(private_key: Ed25519PrivateKey) -> str:
    """导出可配置的原始公钥；调用方必须自行安全保管私钥。"""

    public_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return _encode_base64url(public_bytes)


def encode_ed25519_private_key(private_key: Ed25519PrivateKey) -> str:
    """导出本地控制器配置使用的原始私钥；不得记录或提交返回值。"""

    private_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    return _encode_base64url(private_bytes)


def decode_ed25519_private_key(encoded_private_key: str) -> Ed25519PrivateKey:
    """只供独立签发进程加载私钥，订单服务不会调用此函数。"""

    private_bytes = _decode_base64url(
        encoded_private_key,
        expected_length=32,
        field_name="Ed25519 私钥",
    )
    return Ed25519PrivateKey.from_private_bytes(private_bytes)


def issue_runtime_inventory_proof(
    payload: RuntimeInventoryProofPayload,
    *,
    key_id: str,
    private_key: Ed25519PrivateKey,
) -> RuntimeInventoryProofEnvelope:
    """供独立部署控制器调用；订单服务运行路径不会持有或调用私钥。"""

    signature = private_key.sign(canonical_runtime_inventory_payload(payload))
    return RuntimeInventoryProofEnvelope(
        key_id=key_id,
        payload=payload,
        signature=_encode_base64url(signature),
    )


def parse_runtime_inventory_proof(
    proof_json: str,
    public_keys_json: str,
) -> tuple[RuntimeInventoryProofEnvelope, RuntimeProofPublicKeyRing]:
    """把环境中的两个严格 JSON 文档转换为不可变契约。"""

    return (
        RuntimeInventoryProofEnvelope.model_validate_json(proof_json),
        parse_runtime_proof_public_keys(public_keys_json),
    )


def parse_runtime_proof_public_keys(public_keys_json: str) -> RuntimeProofPublicKeyRing:
    """独立解析预置公钥环，动态刷新时不会信任控制器返回的新公钥。"""

    return RuntimeProofPublicKeyRing.model_validate_json(public_keys_json)


def verify_runtime_inventory_proof(
    envelope: RuntimeInventoryProofEnvelope,
    key_ring: RuntimeProofPublicKeyRing,
    *,
    current_time: datetime,
) -> VerifiedRuntimeInventoryProof:
    """先验签再检查时效，任何未知密钥、篡改或过期都失败关闭。"""

    encoded_public_key = key_ring.keys.get(envelope.key_id)
    if encoded_public_key is None:
        raise RuntimeInventoryProofVerificationError(
            "runtime_inventory_proof_invalid",
            "运行证明引用了未知公钥",
        )
    public_key_bytes = _decode_base64url(
        encoded_public_key,
        expected_length=32,
        field_name="Ed25519 公钥",
    )
    signature = _decode_base64url(
        envelope.signature,
        expected_length=64,
        field_name="Ed25519 签名",
    )
    try:
        Ed25519PublicKey.from_public_bytes(public_key_bytes).verify(
            signature,
            canonical_runtime_inventory_payload(envelope.payload),
        )
    except (InvalidSignature, ValueError) as exc:
        raise RuntimeInventoryProofVerificationError(
            "runtime_inventory_proof_invalid",
            "运行证明签名无效",
        ) from exc
    if current_time.tzinfo is None or current_time.utcoffset() is None:
        raise ValueError("验签时钟必须包含时区")
    normalized_time = current_time.astimezone(UTC)
    if normalized_time < envelope.payload.issued_at:
        raise RuntimeInventoryProofVerificationError(
            "runtime_inventory_proof_not_yet_valid",
            "运行证明尚未生效",
        )
    if normalized_time >= envelope.payload.expires_at:
        raise RuntimeInventoryProofVerificationError(
            "runtime_inventory_proof_expired",
            "运行证明已经过期",
        )
    return VerifiedRuntimeInventoryProof(
        envelope=envelope,
        summary=RuntimeInventoryProofSummary(
            proof_id=envelope.payload.proof_id,
            key_id=envelope.key_id,
            issued_at=envelope.payload.issued_at,
            expires_at=envelope.payload.expires_at,
        ),
    )


def bind_proof_to_runtime_identity(
    verified: VerifiedRuntimeInventoryProof,
    identity: RuntimeArtifactIdentity,
) -> None:
    """证明必须精确包含当前进程，防止为其他工作负载复用合法证明。"""

    matches = [
        instance
        for instance in verified.instances
        if instance.instance_id == identity.instance_id
        and instance.image_repository == identity.image_repository
        and instance.image_digest == identity.image_digest
    ]
    if len(matches) != 1:
        raise RuntimeInventoryProofVerificationError(
            "runtime_inventory_proof_invalid",
            "运行证明没有精确绑定当前进程身份",
        )
