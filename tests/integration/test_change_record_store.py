"""变更记录 PostgreSQL 仓储集成测试。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.change_records import (
    ChangeRecordIdempotencyConflict,
    ChangeRecordStore,
    ConfigurationChange,
    DeploymentChange,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


async def test_store_persists_and_lists_records_in_descending_order(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = ChangeRecordStore(database_session_factory)
    older = await store.record(
        ConfigurationChange(
            external_change_id="pool-change-1",
            from_value=50,
            to_value=5,
        ),
        occurred_at=NOW - timedelta(minutes=10),
        recorded_at=NOW - timedelta(minutes=9),
    )
    newer = await store.record(
        DeploymentChange(
            external_change_id="release-change-1",
            from_version="stable",
            to_version="rc1",
        ),
        occurred_at=NOW - timedelta(minutes=2),
        recorded_at=NOW - timedelta(minutes=1),
    )

    page = await store.list_recent(
        service_name="order-service",
        since=NOW - timedelta(hours=1),
        until=NOW,
    )

    assert page.truncated is False
    assert [item.record_id for item in page.items] == [
        newer.record.record_id,
        older.record.record_id,
    ]


async def test_same_external_id_is_idempotent_but_content_drift_conflicts(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = ChangeRecordStore(database_session_factory)
    change = DeploymentChange(
        external_change_id="release-change-1",
        from_version="stable",
        to_version="rc1",
    )
    created = await store.record(change, occurred_at=NOW, recorded_at=NOW)
    duplicate = await store.record(
        change,
        occurred_at=NOW + timedelta(minutes=1),
        recorded_at=NOW + timedelta(minutes=1),
    )

    assert created.deduplicated is False
    assert duplicate.deduplicated is True
    assert duplicate.record == created.record

    with pytest.raises(ChangeRecordIdempotencyConflict):
        await store.record(
            DeploymentChange(
                external_change_id="release-change-1",
                from_version="stable",
                to_version="rc2",
            ),
            occurred_at=NOW,
            recorded_at=NOW,
        )


async def test_concurrent_same_change_creates_one_record(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = ChangeRecordStore(database_session_factory)
    change = DeploymentChange(
        external_change_id="release-concurrent-1",
        from_version="stable",
        to_version="rc1",
    )

    results = await asyncio.gather(
        *(store.record(change, occurred_at=NOW, recorded_at=NOW) for _ in range(4))
    )

    assert sum(not result.deduplicated for result in results) == 1
    assert len({result.record.record_id for result in results}) == 1


async def test_list_reports_truncation_without_returning_unbounded_records(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = ChangeRecordStore(database_session_factory)
    for index in range(3):
        await store.record(
            DeploymentChange(
                external_change_id=f"release-{index}",
                from_version="stable",
                to_version=f"rc{index}",
            ),
            occurred_at=NOW + timedelta(seconds=index),
            recorded_at=NOW + timedelta(seconds=index),
        )

    page = await store.list_recent(
        service_name="order-service",
        since=NOW - timedelta(minutes=1),
        until=NOW + timedelta(minutes=1),
        limit=2,
    )

    assert len(page.items) == 2
    assert page.truncated is True
