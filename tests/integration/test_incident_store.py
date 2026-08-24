import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.database import Database
from opsswarm.incidents import (
    ActorType,
    AlertSubmission,
    IncidentAction,
    IncidentActor,
    IncidentEventType,
    IncidentSeverity,
    IncidentStatus,
    IncidentVersionConflict,
)
from opsswarm.incidents.persistence import IncidentStore, StoredIncident
from opsswarm.incidents.persistence.tables import AlertRow, IncidentEventRow, IncidentRow

pytestmark = pytest.mark.integration

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
HUMAN_ACTOR = IncidentActor(actor_type=ActorType.HUMAN, actor_id="engineer-001")


def make_submission(
    external_event_id: str,
    *,
    fingerprint: str = "order-service:http-500",
    second_offset: int = 0,
) -> AlertSubmission:
    return AlertSubmission(
        source="fault-simulator",
        external_event_id=external_event_id,
        fingerprint=fingerprint,
        service_name="order-service",
        severity=IncidentSeverity.P1,
        title="订单接口大量返回 500",
        payload={"status_code": 500, "event_id": external_event_id},
        occurred_at=BASE_TIME + timedelta(seconds=second_offset),
        ingested_at=BASE_TIME + timedelta(seconds=second_offset + 1),
    )


async def table_counts(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[int, int, int]:
    async with session_factory() as session:
        incident_count = await session.scalar(select(func.count()).select_from(IncidentRow))
        alert_count = await session.scalar(select(func.count()).select_from(AlertRow))
        event_count = await session.scalar(select(func.count()).select_from(IncidentEventRow))
    assert incident_count is not None
    assert alert_count is not None
    assert event_count is not None
    return incident_count, alert_count, event_count


@pytest.mark.asyncio
async def test_first_alert_persists_incident_alert_and_creation_event(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = IncidentStore(database_session_factory)

    result = await store.ingest_alert(make_submission("alert-001"))
    events = await store.list_events(result.incident.id)

    assert result.created_incident is True
    assert result.deduplicated is False
    assert result.display_number == 1
    assert result.incident.status is IncidentStatus.OPEN
    assert await table_counts(database_session_factory) == (1, 1, 1)
    assert [event.event.event_type for event in events] == [IncidentEventType.CREATED]
    assert events[0].payload["alert_id"] == str(result.alert_id)


@pytest.mark.asyncio
async def test_same_external_event_returns_original_result_without_new_rows(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = IncidentStore(database_session_factory)
    submission = make_submission("alert-001")

    first = await store.ingest_alert(submission)
    duplicate = await store.ingest_alert(submission)

    assert duplicate.deduplicated is True
    assert duplicate.created_incident is False
    assert duplicate.incident.id == first.incident.id
    assert duplicate.alert_id == first.alert_id
    assert await table_counts(database_session_factory) == (1, 1, 1)


@pytest.mark.asyncio
async def test_new_database_engine_restores_persisted_incident(
    database_session_factory: async_sessionmaker[AsyncSession],
    migrated_database_url: str,
) -> None:
    first_store = IncidentStore(database_session_factory)
    created = await first_store.ingest_alert(make_submission("alert-001"))

    restarted_database = Database(migrated_database_url)
    restarted_store = IncidentStore(restarted_database.session_factory)
    try:
        restored = await restarted_store.get_incident(created.incident.id)
        restored_events = await restarted_store.list_events(created.incident.id)
    finally:
        await restarted_database.dispose()

    assert restored.incident == created.incident
    assert restored.display_number == created.display_number
    assert len(restored_events) == 1


@pytest.mark.asyncio
async def test_new_event_with_same_fingerprint_attaches_to_active_incident(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = IncidentStore(database_session_factory)

    first = await store.ingest_alert(make_submission("alert-001"))
    attached = await store.ingest_alert(make_submission("alert-002", second_offset=2))
    events = await store.list_events(first.incident.id)

    assert attached.incident.id == first.incident.id
    assert attached.created_incident is False
    assert attached.deduplicated is False
    assert attached.incident.version == 1
    assert await table_counts(database_session_factory) == (1, 2, 2)
    assert [event.event.event_type for event in events] == [
        IncidentEventType.CREATED,
        IncidentEventType.ALERT_ATTACHED,
    ]
    assert events[0].cursor < events[1].cursor


@pytest.mark.asyncio
async def test_concurrent_alerts_with_same_fingerprint_create_one_active_incident(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = IncidentStore(database_session_factory)
    submissions = [
        make_submission(f"alert-{number:03d}", second_offset=number) for number in range(1, 9)
    ]

    results = await asyncio.gather(*(store.ingest_alert(item) for item in submissions))

    assert len({result.incident.id for result in results}) == 1
    assert sum(result.created_incident for result in results) == 1
    assert await table_counts(database_session_factory) == (1, 8, 8)


@pytest.mark.asyncio
async def test_concurrent_duplicate_events_are_fully_idempotent(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = IncidentStore(database_session_factory)
    submission = make_submission("alert-001")

    results = await asyncio.gather(*(store.ingest_alert(submission) for _ in range(8)))

    assert len({result.incident.id for result in results}) == 1
    assert len({result.alert_id for result in results}) == 1
    assert sum(not result.deduplicated for result in results) == 1
    assert await table_counts(database_session_factory) == (1, 1, 1)


@pytest.mark.asyncio
async def test_transition_updates_version_and_appends_event_atomically(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = IncidentStore(database_session_factory)
    created = await store.ingest_alert(make_submission("alert-001"))

    investigating = await store.transition(
        created.incident.id,
        action=IncidentAction.START_INVESTIGATION,
        expected_version=1,
        occurred_at=BASE_TIME + timedelta(seconds=3),
        actor=HUMAN_ACTOR,
        reason="工程师开始调查",
    )

    assert investigating.incident.status is IncidentStatus.INVESTIGATING
    assert investigating.incident.version == 2
    events = await store.list_events(created.incident.id)
    assert len(events) == 2
    assert events[-1].event.event_type is IncidentEventType.STATUS_CHANGED
    assert events[-1].payload == {"action": IncidentAction.START_INVESTIGATION.value}

    with pytest.raises(IncidentVersionConflict):
        await store.transition(
            created.incident.id,
            action=IncidentAction.START_INVESTIGATION,
            expected_version=1,
            occurred_at=BASE_TIME + timedelta(seconds=4),
            actor=HUMAN_ACTOR,
            reason="过期页面重复提交",
        )

    persisted = await store.get_incident(created.incident.id)
    assert persisted.incident.version == 2
    assert len(await store.list_events(created.incident.id)) == 2


@pytest.mark.asyncio
async def test_concurrent_transitions_allow_only_one_version_winner(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = IncidentStore(database_session_factory)
    created = await store.ingest_alert(make_submission("alert-001"))

    async def start_investigation(reason: str) -> object:
        try:
            return await store.transition(
                created.incident.id,
                action=IncidentAction.START_INVESTIGATION,
                expected_version=1,
                occurred_at=BASE_TIME + timedelta(seconds=3),
                actor=HUMAN_ACTOR,
                reason=reason,
            )
        except IncidentVersionConflict as error:
            return error

    outcomes: Sequence[object] = await asyncio.gather(
        start_investigation("并发操作者 A"),
        start_investigation("并发操作者 B"),
    )

    assert sum(isinstance(item, StoredIncident) for item in outcomes) == 1
    assert sum(isinstance(item, IncidentVersionConflict) for item in outcomes) == 1
    persisted = await store.get_incident(created.incident.id)
    assert persisted.incident.version == 2
    assert len(await store.list_events(created.incident.id)) == 2


@pytest.mark.asyncio
async def test_closed_incident_allows_new_incident_for_same_fingerprint(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = IncidentStore(database_session_factory)
    first = await store.ingest_alert(make_submission("alert-001"))
    transitions = [
        (IncidentAction.START_INVESTIGATION, 3),
        (IncidentAction.START_VERIFICATION, 4),
        (IncidentAction.CONFIRM_RECOVERY, 5),
        (IncidentAction.CLOSE, 6),
    ]
    version = 1
    for action, second_offset in transitions:
        stored = await store.transition(
            first.incident.id,
            action=action,
            expected_version=version,
            occurred_at=BASE_TIME + timedelta(seconds=second_offset),
            actor=HUMAN_ACTOR,
            reason="推进事故生命周期",
        )
        version = stored.incident.version

    second = await store.ingest_alert(make_submission("alert-002", second_offset=10))

    assert first.incident.id != second.incident.id
    assert second.created_incident is True
    assert await table_counts(database_session_factory) == (2, 2, 6)
