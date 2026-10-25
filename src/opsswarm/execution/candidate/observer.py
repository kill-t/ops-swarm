"""固定本地 GET 的异步有界运行参数观测适配器。"""

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Protocol

import httpx2

from opsswarm.execution.candidate.contracts import OBSERVATION_PATHS, RuntimeObservation
from opsswarm.execution.candidate.policy import validate_step
from opsswarm.execution.settings import ExecutionSettings
from opsswarm.fault_lab.models import DatabasePoolStatus, FaultStatus, RedisDependencyStatus
from opsswarm.planning.models import ActionStep, ActionType

MAX_RESPONSE_BYTES = 64 * 1024


def utc_now() -> datetime:
    return datetime.now(UTC)


class PreflightObserver(Protocol):
    """只读观测端口；不接受任意 URL、命令或持久化执行步骤。"""

    async def observe(self, step: ActionStep) -> RuntimeObservation: ...


class FaultLabPreflightObserver:
    """单次观测覆盖连接、读取和关闭的总超时，不回退冻结材料。"""

    def __init__(
        self,
        settings: ExecutionSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        provided = settings if settings is not None else ExecutionSettings()
        # 接受配置值而非调用方对象的可信身份，防止未校验的复制绕过本地地址护栏。
        self._settings = ExecutionSettings.model_validate(provided.model_dump())
        self._transport, self._clock = transport, clock

    async def observe(self, step: ActionStep) -> RuntimeObservation:
        validate_step(step)
        path = OBSERVATION_PATHS[step.action_type]
        try:
            async with asyncio.timeout(self._settings.request_timeout_seconds):
                async with (
                    httpx2.AsyncClient(
                        base_url=self._settings.fault_lab_base_url,
                        timeout=self._settings.request_timeout_seconds,
                        transport=self._transport,
                        trust_env=False,
                        follow_redirects=False,
                        headers={"Accept-Encoding": "identity"},
                    ) as client,
                    client.stream("GET", path) as response,
                ):
                    response.raise_for_status()
                    payload = await _bounded_body(response)
                if step.action_type is ActionType.ROLLBACK_CONFIGURATION:
                    pool = DatabasePoolStatus.model_validate_json(payload, strict=True)
                    return RuntimeObservation(
                        action_type=step.action_type,
                        observed_at=self._clock(),
                        source_path=path,
                        current_value=pool.value,
                    )
                if step.action_type is ActionType.RESTORE_DEPENDENCY:
                    redis = RedisDependencyStatus.model_validate_json(payload, strict=True)
                    return RuntimeObservation(
                        action_type=step.action_type,
                        observed_at=self._clock(),
                        source_path=path,
                        available=redis.available,
                    )
                release = FaultStatus.model_validate_json(payload, strict=True)
                return RuntimeObservation(
                    action_type=step.action_type,
                    observed_at=self._clock(),
                    source_path=path,
                    release_enabled=release.enabled,
                )
        except (TimeoutError, httpx2.TimeoutException):
            code = "fault_lab_timeout"
        except httpx2.HTTPError:
            code = "fault_lab_unavailable"
        except (ValueError, TypeError):
            code = "fault_lab_invalid_response"
        return RuntimeObservation(
            action_type=step.action_type,
            observed_at=self._clock(),
            source_path=path,
            error_code=code,
        )


async def _bounded_body(response: httpx2.Response) -> bytes:
    """禁止压缩扩张，并在累积每个分块前检查响应上限。"""

    if response.headers.get("content-encoding", "identity").lower() != "identity":
        raise ValueError("不接受压缩观测响应")
    length = response.headers.get("content-length")
    if length is not None and (int(length) < 0 or int(length) > MAX_RESPONSE_BYTES):
        raise ValueError("观测响应超过上限")
    result = bytearray()
    async for chunk in response.aiter_bytes(chunk_size=8192):
        if len(result) + len(chunk) > MAX_RESPONSE_BYTES:
            raise ValueError("观测响应超过上限")
        result.extend(chunk)
    return bytes(result)
