"""SSE 从真实 PostgreSQL 时间线按全局游标补发的集成测试。"""

import json

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.control_plane.sse import IncidentEventStream
from opsswarm.incidents.persistence import IncidentStore
from tests.integration.test_incident_store import make_submission

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def never_disconnected() -> bool:
    return False


async def test_stream_replays_only_events_after_last_event_id(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = IncidentStore(database_session_factory)
    first = await store.ingest_alert(make_submission("sse-alert-001"))
    await store.ingest_alert(make_submission("sse-alert-002", second_offset=1))
    events = await store.list_events(first.incident.id)
    stream = IncidentEventStream(store).stream(
        first.incident.id,
        after_cursor=events[0].cursor,
        is_disconnected=never_disconnected,
    )

    block = await anext(stream)
    await stream.aclose()

    lines = block.splitlines()
    assert lines[0] == f"id: {events[1].cursor}"
    assert lines[1] == "event: incident.alert_attached"
    data = json.loads(next(line for line in lines if line.startswith("data: "))[6:])
    assert data["cursor"] == events[1].cursor
    assert data["incident_id"] == str(first.incident.id)
