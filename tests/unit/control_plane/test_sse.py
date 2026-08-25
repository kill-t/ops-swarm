"""SSE 编码、游标重放、心跳和断开行为测试。"""

import json
from collections.abc import Sequence
from datetime import UTC, datetime
from uuid import UUID

import pytest
from pydantic import ValidationError

from opsswarm.control_plane.sse import (
    IncidentEventStream,
    IncidentEventStreamSettings,
    format_sse_event,
)
from opsswarm.incidents import (
    ActorType,
    IncidentActor,
    IncidentEvent,
    IncidentEventType,
    IncidentStatus,
)
from opsswarm.incidents.persistence import StoredIncidentEvent

INCIDENT_ID = UUID("00000000-0000-0000-0000-000000000501")
BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def stored_event(cursor: int, *, reason: str = "收到告警") -> StoredIncidentEvent:
    return StoredIncidentEvent(
        cursor=cursor,
        event=IncidentEvent(
            event_type=(
                IncidentEventType.CREATED if cursor == 1 else IncidentEventType.ALERT_ATTACHED
            ),
            incident_id=INCIDENT_ID,
            actor=IncidentActor(actor_type=ActorType.SYSTEM),
            from_status=None,
            to_status=IncidentStatus.OPEN,
            reason=reason,
            occurred_at=BASE_TIME,
            incident_version=1,
        ),
        payload={"source": "故障脚本", "cursor": cursor},
    )


class FakeEventSource:
    def __init__(self, events: Sequence[StoredIncidentEvent]) -> None:
        self.events = tuple(events)
        self.calls: list[tuple[int, int]] = []

    async def list_events(
        self,
        incident_id: UUID,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> Sequence[StoredIncidentEvent]:
        assert incident_id == INCIDENT_ID
        self.calls.append((after_cursor, limit))
        return tuple(item for item in self.events if item.cursor > after_cursor)[:limit]


class ManualMonotonic:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


class ControlledConnection:
    def __init__(self) -> None:
        self.disconnected = False

    async def check(self) -> bool:
        return self.disconnected


def parse_sse_data(block: str) -> dict[str, object]:
    data_line = next(line for line in block.splitlines() if line.startswith("data: "))
    parsed = json.loads(data_line.removeprefix("data: "))
    assert isinstance(parsed, dict)
    return parsed


def test_format_sse_event_uses_persisted_cursor_and_single_json_data_line() -> None:
    block = format_sse_event(stored_event(1, reason="第一行\n第二行"), retry_milliseconds=3000)

    assert block.startswith("id: 1\nevent: incident.created\nretry: 3000\ndata: ")
    assert block.endswith("\n\n")
    assert len([line for line in block.splitlines() if line.startswith("data: ")]) == 1
    data = parse_sse_data(block)
    assert data["cursor"] == 1
    assert data["reason"] == "第一行\n第二行"
    assert data["payload"] == {"cursor": 1, "source": "故障脚本"}


@pytest.mark.asyncio
async def test_stream_replays_after_cursor_then_heartbeats_without_changing_cursor() -> None:
    source = FakeEventSource((stored_event(1), stored_event(2)))
    monotonic = ManualMonotonic()
    connection = ControlledConnection()

    async def advance_time(seconds: float) -> None:
        monotonic.value += seconds

    stream = IncidentEventStream(
        source,
        IncidentEventStreamSettings(
            batch_size=100,
            poll_interval_seconds=1,
            heartbeat_interval_seconds=5,
            retry_milliseconds=1000,
        ),
        sleep=advance_time,
        monotonic=monotonic,
    ).stream(
        INCIDENT_ID,
        after_cursor=1,
        is_disconnected=connection.check,
    )

    replayed = await anext(stream)
    heartbeat = await anext(stream)
    connection.disconnected = True

    assert replayed.startswith("id: 2\n")
    assert parse_sse_data(replayed)["cursor"] == 2
    assert heartbeat == ": heartbeat\n\n"
    assert source.calls[0] == (1, 100)
    assert all(after_cursor == 2 for after_cursor, _ in source.calls[1:])
    with pytest.raises(StopAsyncIteration):
        await anext(stream)


@pytest.mark.parametrize(
    "overrides",
    [
        {"batch_size": 0},
        {"poll_interval_seconds": 0},
        {"heartbeat_interval_seconds": 4},
        {"retry_milliseconds": 99},
    ],
)
def test_stream_settings_reject_unsafe_ranges(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        IncidentEventStreamSettings(**overrides)
