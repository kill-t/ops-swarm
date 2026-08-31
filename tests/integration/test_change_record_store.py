"""变更记录 PostgreSQL 仓储集成测试。"""

import asyncio
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.change_records import (
    CI_DEPLOYMENT_SOURCE,
    ChangeRecordIdempotencyConflict,
    ChangeRecordStore,
    ConfigurationChange,
    DeploymentChange,
    DeploymentCodeReference,
)
from opsswarm.change_records.persistence.tables import ChangeRecordRow

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)
COMMIT_SHA = "a" * 40


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
            code_reference=DeploymentCodeReference(
                repository="kill-t/ops-swarm",
                commit_sha=COMMIT_SHA,
            ),
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
    assert page.items[0].change == newer.record.change


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


async def test_ci_source_is_isolated_and_rejects_deployment_time_drift(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store = ChangeRecordStore(database_session_factory)
    change = DeploymentChange(
        external_change_id="shared-delivery-1",
        from_version="stable",
        to_version="rc1",
        code_reference=DeploymentCodeReference(
            repository="kill-t/ops-swarm",
            commit_sha=COMMIT_SHA,
        ),
    )
    local = await store.record(change, occurred_at=NOW, recorded_at=NOW)
    ci = await store.record(
        change,
        occurred_at=NOW - timedelta(minutes=1),
        recorded_at=NOW,
        source=CI_DEPLOYMENT_SOURCE,
        source_identity="github-actions-production",
    )

    assert local.record.record_id != ci.record.record_id
    assert ci.record.source == CI_DEPLOYMENT_SOURCE
    assert ci.record.source_identity == "github-actions-production"

    with pytest.raises(ChangeRecordIdempotencyConflict):
        await store.record(
            change,
            occurred_at=NOW - timedelta(minutes=2),
            recorded_at=NOW,
            source=CI_DEPLOYMENT_SOURCE,
            source_identity="github-actions-production",
        )

    with pytest.raises(ChangeRecordIdempotencyConflict):
        await store.record(
            change,
            occurred_at=NOW - timedelta(minutes=1),
            recorded_at=NOW,
            source=CI_DEPLOYMENT_SOURCE,
            source_identity="rotated-pipeline-identity",
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


async def test_database_rejects_malformed_deployment_code_reference(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """即使绕过 Pydantic，PostgreSQL 也不能接收伪造的代码引用。"""

    async with database_session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    insert(ChangeRecordRow).values(
                        id=uuid4(),
                        source="fault-lab-change-simulator",
                        source_identity="local-admin",
                        external_change_id="invalid-code-reference",
                        service_name="order-service",
                        change_type="deployment",
                        details={
                            "change_type": "deployment",
                            "external_change_id": "invalid-code-reference",
                            "from_version": "stable",
                            "to_version": "rc1",
                            "code_reference": {
                                "repository": "kill-t/ops-swarm",
                                "commit_sha": "not-a-sha",
                            },
                            "completed": True,
                        },
                        content_hash="0" * 64,
                        occurred_at=NOW,
                        recorded_at=NOW,
                    )
                )


@pytest.mark.parametrize(
    ("source", "source_identity", "change_type", "details"),
    [
        (
            "fault-lab-change-simulator",
            "github-actions-production",
            "deployment",
            {
                "change_type": "deployment",
                "external_change_id": "invalid-source-identity",
                "from_version": "stable",
                "to_version": "rc1",
                "completed": True,
            },
        ),
        (
            "ci-deployment-ingestion",
            "local-admin",
            "deployment",
            {
                "change_type": "deployment",
                "external_change_id": "invalid-admin-identity",
                "from_version": "stable",
                "to_version": "rc1",
                "code_reference": {
                    "repository": "kill-t/ops-swarm",
                    "commit_sha": COMMIT_SHA,
                },
                "completed": True,
            },
        ),
        (
            "ci-deployment-ingestion",
            "github-actions-production",
            "configuration",
            {
                "change_type": "configuration",
                "external_change_id": "invalid-ci-type",
                "key": "database_pool_size",
                "from_value": 50,
                "to_value": 5,
                "completed": True,
            },
        ),
    ],
)
async def test_database_enforces_source_identity_and_ci_deployment_contract(
    database_session_factory: async_sessionmaker[AsyncSession],
    source: str,
    source_identity: str,
    change_type: str,
    details: dict[str, object],
) -> None:
    async with database_session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    insert(ChangeRecordRow).values(
                        id=uuid4(),
                        source=source,
                        source_identity=source_identity,
                        external_change_id=details["external_change_id"],
                        service_name="order-service",
                        change_type=change_type,
                        details=details,
                        content_hash="0" * 64,
                        occurred_at=NOW,
                        recorded_at=NOW,
                    )
                )
