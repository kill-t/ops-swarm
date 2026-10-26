"""固定本地GET的有界发布版本观测，不提供写入能力。"""

import asyncio
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol

import httpx2

from opsswarm.execution.release.contracts import ReleaseObservation, ReleaseRequestError
from opsswarm.execution.settings import ExecutionSettings

PATH: Literal["/__admin/simulated-release"] = "/__admin/simulated-release"
MAX_RESPONSE_BYTES = 64 * 1024


def utc_now() -> datetime:
    return datetime.now(UTC)


class ReleaseObserver(Protocol):
    async def observe(self) -> ReleaseObservation: ...


class FaultLabReleaseObserver:
    def __init__(
        self,
        settings: ExecutionSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        provided = settings if settings is not None else ExecutionSettings()
        self._settings = ExecutionSettings.model_validate(provided.model_dump())
        self._transport, self._clock = transport, clock

    async def observe(self) -> ReleaseObservation:
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
                    client.stream("GET", PATH) as response,
                ):
                    response.raise_for_status()
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ValueError("不接受压缩观测")
                    length = response.headers.get("content-length")
                    if length is not None and not 0 <= int(length) <= MAX_RESPONSE_BYTES:
                        raise ValueError("发布观测超过上限")
                    payload = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=8192):
                        if len(payload) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise ValueError("发布观测超过上限")
                        payload.extend(chunk)
                raw = json.loads(payload)
                if not isinstance(raw, dict) or raw.get("simulation") is not True:
                    raise ValueError("观测必须明确为本地演练")
                # 先校验原始完整字段，不能由ReleaseState默认值补出远端没有报告的身份。
                return ReleaseObservation.model_validate(
                    dict(state=raw, observed_at=self._clock(), source_path=PATH)
                )
        except (TimeoutError, httpx2.TimeoutException) as error:
            raise ReleaseRequestError(
                "candidate_release_observation_timeout", "发布版本观测超时"
            ) from error
        except httpx2.HTTPError as error:
            raise ReleaseRequestError(
                "candidate_release_observation_unavailable", "发布版本观测不可用"
            ) from error
        except (ValueError, TypeError) as error:
            raise ReleaseRequestError(
                "candidate_release_observation_invalid", "发布版本观测无法严格校验"
            ) from error
