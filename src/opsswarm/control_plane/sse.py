"""从 PostgreSQL 时间线补发并持续轮询的 SSE 传输服务。"""

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Protocol
from uuid import UUID

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.control_plane.schemas import IncidentEventResponse
from opsswarm.incidents.persistence import StoredIncidentEvent

Sleep = Callable[[float], Awaitable[None]]
MonotonicClock = Callable[[], float]
DisconnectCheck = Callable[[], Awaitable[bool]]


class IncidentEventSource(Protocol):
    """SSE 只依赖按游标读取事件的最小仓储能力。"""

    async def list_events(
        self,
        incident_id: UUID,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> Sequence[StoredIncidentEvent]: ...


class IncidentEventStreamSettings(BaseSettings):
    """SSE 轮询、心跳和浏览器重连配置。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_SSE_", extra="ignore")

    batch_size: int = Field(default=100, ge=1, le=500)
    poll_interval_seconds: float = Field(default=1.0, gt=0, le=10)
    heartbeat_interval_seconds: float = Field(default=15.0, ge=5, le=60)
    retry_milliseconds: int = Field(default=3000, ge=100, le=60000)


class IncidentEventStream:
    """不持有数据库连接的可重放 Incident 事件流。"""

    def __init__(
        self,
        source: IncidentEventSource,
        settings: IncidentEventStreamSettings | None = None,
        *,
        sleep: Sleep = asyncio.sleep,
        monotonic: MonotonicClock = time.monotonic,
    ) -> None:
        self._source = source
        self._settings = settings if settings is not None else IncidentEventStreamSettings()
        self._sleep = sleep
        self._monotonic = monotonic

    async def stream(
        self,
        incident_id: UUID,
        *,
        after_cursor: int,
        is_disconnected: DisconnectCheck,
    ) -> AsyncIterator[str]:
        """先补发历史事件，再轮询新事件并定期发送无游标心跳。"""

        if after_cursor < 0:
            raise ValueError("after_cursor 不能小于 0")
        cursor = after_cursor
        last_activity = self._monotonic()
        while not await is_disconnected():
            records = await self._source.list_events(
                incident_id,
                after_cursor=cursor,
                limit=self._settings.batch_size,
            )
            if records:
                for record in records:
                    cursor = record.cursor
                    yield format_sse_event(
                        record,
                        retry_milliseconds=self._settings.retry_milliseconds,
                    )
                last_activity = self._monotonic()
                # 满批说明可能仍有积压，立即继续补发，避免人为增加延迟。
                if len(records) >= self._settings.batch_size:
                    continue
            elif self._monotonic() - last_activity >= self._settings.heartbeat_interval_seconds:
                yield ": heartbeat\n\n"
                last_activity = self._monotonic()
            await self._sleep(self._settings.poll_interval_seconds)


def format_sse_event(record: StoredIncidentEvent, *, retry_milliseconds: int) -> str:
    """把一个已持久化事件编码为不会注入额外 SSE 字段的单行数据块。"""

    if retry_milliseconds < 100:
        raise ValueError("retry_milliseconds 不能小于 100")
    response = IncidentEventResponse.from_stored(record)
    data = json.dumps(
        response.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return (
        f"id: {record.cursor}\n"
        f"event: {record.event.event_type.value}\n"
        f"retry: {retry_milliseconds}\n"
        f"data: {data}\n\n"
    )
