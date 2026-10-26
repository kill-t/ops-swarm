"""固定本地GET的有界发布版本观测，不提供写入能力。"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal, Protocol

import httpx2

from opsswarm.execution.release.contracts import ReleaseObservation, ReleaseRequestError
from opsswarm.execution.release.read_transport import MAX_RESPONSE_BYTES as MAX_RESPONSE_BYTES
from opsswarm.execution.release.read_transport import read_json
from opsswarm.execution.settings import ExecutionSettings

PATH: Literal["/__admin/simulated-release"] = "/__admin/simulated-release"


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
            _, raw = await read_json(self._settings, self._transport, PATH)
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
