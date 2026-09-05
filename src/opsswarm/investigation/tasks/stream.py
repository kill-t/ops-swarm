"""专业任务事件的 SSE 编码，材料通过独立接口按需读取。"""

import asyncio
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from uuid import UUID

from opsswarm.control_plane.replay_stream import replay_events
from opsswarm.investigation.tasks.events import TaskEventRecord
from opsswarm.investigation.tasks.queries import CollaborationQueries


def format_task_event(record: TaskEventRecord) -> str:
    return (
        f"id: {record.cursor}\nevent: {record.event_type}\nretry: 3000\n"
        f"data: {record.model_dump_json()}\n\n"
    )


async def stream_task_events(
    queries: CollaborationQueries,
    run_id: UUID,
    *,
    after_cursor: int,
    is_disconnected: Callable[[], Awaitable[bool]],
) -> AsyncIterator[str]:
    async def fetch(cursor: int, limit: int) -> Sequence[TaskEventRecord]:
        return await queries.list_events(run_id, after_cursor=cursor, limit=limit)

    async for frame in replay_events(
        fetch=fetch,
        encode=format_task_event,
        after_cursor=after_cursor,
        batch_size=100,
        poll_interval=1,
        heartbeat_interval=15,
        is_disconnected=is_disconnected,
        sleep=asyncio.sleep,
        monotonic=time.monotonic,
    ):
        yield frame
