"""供不同业务时间线复用的游标补发、心跳和轮询。"""

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import Protocol


class CursorRecord(Protocol):
    @property
    def cursor(self) -> int: ...


async def replay_events[T: CursorRecord](
    *,
    fetch: Callable[[int, int], Awaitable[Sequence[T]]],
    encode: Callable[[T], str],
    after_cursor: int,
    batch_size: int,
    poll_interval: float,
    heartbeat_interval: float,
    is_disconnected: Callable[[], Awaitable[bool]],
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
) -> AsyncIterator[str]:
    if after_cursor < 0:
        raise ValueError("after_cursor 不能小于 0")
    cursor = after_cursor
    last_activity = monotonic()
    while not await is_disconnected():
        records = await fetch(cursor, batch_size)
        if records:
            for record in records:
                cursor = record.cursor
                yield encode(record)
            last_activity = monotonic()
            if len(records) >= batch_size:
                continue
        elif monotonic() - last_activity >= heartbeat_interval:
            yield ": heartbeat\n\n"
            last_activity = monotonic()
        await sleep(poll_interval)
