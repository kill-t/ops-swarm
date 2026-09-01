"""从固定部署控制器刷新短时运行清单证明。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

import httpx2
from pydantic import ValidationError

from opsswarm.fault_lab.runtime_metadata import (
    RuntimeArtifactIdentity,
    RuntimeInventoryProofErrorCode,
)
from opsswarm.fault_lab.runtime_proof import (
    RuntimeInventoryProofEnvelope,
    RuntimeProofPublicKeyRing,
    VerifiedRuntimeInventoryProof,
    bind_proof_to_runtime_identity,
    verify_runtime_inventory_proof,
)

MAX_RUNTIME_PROOF_REFRESH_RESPONSE_BYTES = 64 * 1024


def _utc_now() -> datetime:
    return datetime.now(UTC)


class RuntimeInventoryProofRefreshFailure(RuntimeError):
    """动态证明无法形成可信结果；调用方不得回退到旧证明。"""

    def __init__(self, message: str = "运行证明动态刷新失败") -> None:
        super().__init__(message)
        self.error_code: RuntimeInventoryProofErrorCode = "runtime_inventory_proof_refresh_failed"
        self.message = message


class RuntimeInventoryProofRefresher(Protocol):
    """订单服务只依赖该只读刷新端口，测试可注入固定实现。"""

    def refresh(
        self,
        *,
        identity: RuntimeArtifactIdentity,
    ) -> VerifiedRuntimeInventoryProof: ...


class HttpRuntimeInventoryProofRefresher:
    """只向已校验的固定控制器地址发送无参数 GET 请求。"""

    def __init__(
        self,
        *,
        base_url: str,
        timeout_seconds: float,
        public_keys: RuntimeProofPublicKeyRing,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds
        self._public_keys = public_keys
        self._transport = transport
        self._clock = clock

    def refresh(
        self,
        *,
        identity: RuntimeArtifactIdentity,
    ) -> VerifiedRuntimeInventoryProof:
        try:
            with (
                httpx2.Client(
                    base_url=self._base_url,
                    timeout=self._timeout_seconds,
                    transport=self._transport,
                    trust_env=False,
                ) as client,
                client.stream("GET", "/api/v1/runtime-inventory-proof") as response,
            ):
                if response.status_code != 200:
                    raise RuntimeInventoryProofRefreshFailure("部署控制器返回了非预期状态")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > MAX_RUNTIME_PROOF_REFRESH_RESPONSE_BYTES:
                        raise RuntimeInventoryProofRefreshFailure("运行证明刷新响应超过 64 KiB")
                    content.extend(chunk)
        except RuntimeInventoryProofRefreshFailure:
            raise
        except (httpx2.TimeoutException, httpx2.HTTPError) as exc:
            raise RuntimeInventoryProofRefreshFailure() from exc
        try:
            envelope = RuntimeInventoryProofEnvelope.model_validate_json(content)
        except (ValidationError, ValueError) as exc:
            raise RuntimeInventoryProofRefreshFailure("部署控制器返回了非法证明") from exc
        verified = verify_runtime_inventory_proof(
            envelope,
            self._public_keys,
            # 必须在接收证明后取时间，否则新证明会被误判为尚未生效。
            current_time=self._clock(),
        )
        bind_proof_to_runtime_identity(verified, identity)
        return verified
