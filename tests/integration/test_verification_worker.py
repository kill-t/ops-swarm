"""恢复验证调度租约、崩溃重放与自动时间窗口的真实 PostgreSQL 测试。"""

import asyncio
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents import IncidentStatus
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.verification import (
    VerificationStatus,
    VerificationWorkItemLeaseLost,
    VerificationWorkItemStatus,
)
from opsswarm.verification.persistence import (
    VerificationStore,
    scheduled_sample_idempotency_key,
)
from opsswarm.verification.service import VerificationService
from opsswarm.verification.worker import VerificationWorker
from tests.integration.test_investigation_store import BASE_TIME
from tests.integration.test_verification_store import HealthyProbe, prepare_successful_execution

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class MutableClock:
    """由测试显式推进，验证调度不会依赖真实等待。"""

    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


async def create_scheduled_run(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> tuple[VerificationStore, VerificationService, HealthyProbe, object]:
    execution, started = await prepare_successful_execution(session_factory, suffix=suffix)
    store = VerificationStore(session_factory)
    probe = HealthyProbe()
    service = VerificationService(
        store,
        probe,
        clock=lambda: BASE_TIME + timedelta(seconds=8),
    )
    creation = await service.create_run(execution.id, expected_incident_version=5)
    return store, service, probe, (creation.run, started)


async def test_creation_is_atomic_and_concurrent_workers_claim_only_once(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, _, _, context = await create_scheduled_run(
        database_session_factory,
        suffix="scheduler-claim",
    )
    run, _ = context
    pending = await store.get_work_item(run.id)

    claims = await asyncio.gather(
        store.claim_next_work_item(
            worker_id="verification-worker-001",
            occurred_at=BASE_TIME + timedelta(seconds=8),
            lease_duration=timedelta(seconds=10),
        ),
        store.claim_next_work_item(
            worker_id="verification-worker-002",
            occurred_at=BASE_TIME + timedelta(seconds=8),
            lease_duration=timedelta(seconds=10),
        ),
    )

    assert pending.id == run.id
    assert pending.status is VerificationWorkItemStatus.PENDING
    assert pending.next_sample_sequence == 1
    assert len([item for item in claims if item is not None]) == 1


async def test_expired_lease_reuses_persisted_sample_without_second_probe(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, service, probe, context = await create_scheduled_run(
        database_session_factory,
        suffix="scheduler-replay",
    )
    run, _ = context
    first_claim = await store.claim_next_work_item(
        worker_id="verification-worker-crashed",
        occurred_at=BASE_TIME + timedelta(seconds=8),
        lease_duration=timedelta(seconds=10),
    )
    assert first_claim is not None and first_claim.lease_token is not None
    await service.sample(
        run.id,
        idempotency_key=scheduled_sample_idempotency_key(
            first_claim.id,
            first_claim.next_sample_sequence,
        ),
    )
    assert probe.call_count == 1

    clock = MutableClock(BASE_TIME + timedelta(seconds=19))
    recovery_service = VerificationService(store, probe, clock=clock)
    recovery_worker = VerificationWorker(
        store=store,
        service=recovery_service,
        worker_id="verification-worker-recovery",
        clock=clock,
        lease_duration=timedelta(seconds=10),
    )
    assert await recovery_worker.run_once() is True

    recovered = await store.get_work_item(run.id)
    stored_run = await store.get(run.id)
    assert probe.call_count == 1
    assert len(stored_run.samples) == 1
    assert recovered.status is VerificationWorkItemStatus.PENDING
    assert recovered.next_sample_sequence == 2
    assert recovered.attempt_count == 2
    assert recovered.available_at == BASE_TIME + timedelta(seconds=38)

    with pytest.raises(VerificationWorkItemLeaseLost):
        await store.settle_work_item(
            first_claim.id,
            lease_token=first_claim.lease_token,
            occurred_at=BASE_TIME + timedelta(seconds=19),
        )


async def test_worker_automatically_spans_window_and_resolves_incident(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, _, probe, context = await create_scheduled_run(
        database_session_factory,
        suffix="scheduler-window",
    )
    run, started = context
    clock = MutableClock(BASE_TIME + timedelta(seconds=8))
    service = VerificationService(store, probe, clock=clock)
    worker = VerificationWorker(
        store=store,
        service=service,
        worker_id="verification-worker-window",
        clock=clock,
        lease_duration=timedelta(seconds=10),
    )

    assert await worker.run_once() is True
    assert await worker.run_once() is False
    clock.value = BASE_TIME + timedelta(seconds=38)
    assert await worker.run_once() is True
    clock.value = BASE_TIME + timedelta(seconds=68)
    assert await worker.run_once() is True

    completed_run = await store.get(run.id)
    completed_work = await store.get_work_item(run.id)
    incident = await IncidentStore(database_session_factory).get_incident(
        started.incident.incident.id
    )
    assert completed_run.status is VerificationStatus.PASSED
    assert completed_run.successful_sample_count == 3
    assert [sample.observed_at for sample in completed_run.samples] == [
        BASE_TIME + timedelta(seconds=8),
        BASE_TIME + timedelta(seconds=38),
        BASE_TIME + timedelta(seconds=68),
    ]
    assert completed_work.status is VerificationWorkItemStatus.COMPLETED
    assert completed_work.completed_at == BASE_TIME + timedelta(seconds=68)
    assert probe.call_count == 3
    assert incident.incident.status is IncidentStatus.RESOLVED
