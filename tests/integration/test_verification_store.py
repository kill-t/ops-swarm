"""逐次恢复采样、时间窗口和 Incident 结论的真实 PostgreSQL 测试。"""

from collections import deque
from collections.abc import Iterable
from datetime import datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.execution import OperationAdapterFailure, OperationResult
from opsswarm.execution.persistence import OperationExecutionStore, StoredOperationExecution
from opsswarm.execution.service import OperationExecutionService
from opsswarm.incidents import IncidentStatus
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.persistence import StartedInvestigation
from opsswarm.planning.persistence import StoredActionStep
from opsswarm.verification import (
    OperationExecutionNotVerifiable,
    VerificationObservation,
    VerificationProbeFailure,
    VerificationRunCompleted,
    VerificationSampleStatus,
    VerificationStatus,
)
from opsswarm.verification.persistence import VerificationStore
from opsswarm.verification.service import VerificationService
from tests.integration.test_investigation_store import BASE_TIME
from tests.integration.test_operation_execution_store import (
    SuccessfulAdapter,
    prepare_approved_step,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class SequenceClock:
    """按调用顺序提供确定时间，避免测试真实等待观察窗口。"""

    def __init__(self, values: Iterable[datetime]) -> None:
        self._values = deque(values)

    def __call__(self) -> datetime:
        return self._values.popleft()


class HealthyProbe:
    def __init__(self) -> None:
        self.call_count = 0

    async def observe(self, step: StoredActionStep) -> VerificationObservation:
        self.call_count += 1
        return VerificationObservation(
            signal=step.verification.signal,
            value=0.0,
            http_status=200,
        )


class UnhealthyProbe:
    async def observe(self, step: StoredActionStep) -> VerificationObservation:
        return VerificationObservation(
            signal=step.verification.signal,
            value=1.0,
            http_status=500,
        )


class BrokenProbe:
    async def observe(self, step: StoredActionStep) -> VerificationObservation:
        del step
        raise VerificationProbeFailure(
            error_code="verification_probe_unavailable",
            message="恢复验证探针不可用",
        )


class FailingOperationAdapter:
    async def execute(self, step: StoredActionStep) -> OperationResult:
        del step
        raise OperationAdapterFailure(
            error_code="fault_lab_unavailable",
            message="故障演练服务不可用",
        )


async def prepare_successful_execution(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> tuple[StoredOperationExecution, StartedInvestigation]:
    started, _, step = await prepare_approved_step(session_factory, suffix=suffix)
    outcome = await OperationExecutionService(
        OperationExecutionStore(session_factory),
        SuccessfulAdapter(),
        clock=lambda: BASE_TIME + timedelta(seconds=7),
    ).execute_step(
        step.id,
        idempotency_key=f"verify-operation-{suffix}",
        expected_incident_version=4,
        occurred_at=BASE_TIME + timedelta(seconds=7),
    )
    return outcome.execution, started


async def test_three_successes_spanning_window_resolve_incident_and_retry_is_idempotent(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution, started = await prepare_successful_execution(
        database_session_factory,
        suffix="passed",
    )
    probe = HealthyProbe()
    service = VerificationService(
        VerificationStore(database_session_factory),
        probe,
        clock=SequenceClock(
            [
                BASE_TIME + timedelta(seconds=8),
                BASE_TIME + timedelta(seconds=8),
                BASE_TIME + timedelta(seconds=38),
                BASE_TIME + timedelta(seconds=68),
            ]
        ),
    )

    creation = await service.create_run(
        execution.id,
        expected_incident_version=5,
    )
    first = await service.sample(creation.run.id, idempotency_key="verify-sample-001")
    second = await service.sample(creation.run.id, idempotency_key="verify-sample-002")
    third = await service.sample(creation.run.id, idempotency_key="verify-sample-003")
    duplicate = await service.sample(creation.run.id, idempotency_key="verify-sample-003")

    assert creation.created is True
    assert first.run.status is VerificationStatus.RUNNING
    assert second.run.status is VerificationStatus.RUNNING
    assert third.run.status is VerificationStatus.PASSED
    assert third.run.successful_sample_count == 3
    assert [item.sequence for item in third.run.samples] == [1, 2, 3]
    assert duplicate.created is False
    assert duplicate.probe_performed is False
    assert probe.call_count == 3
    incident = await IncidentStore(database_session_factory).get_incident(
        started.incident.incident.id
    )
    assert incident.incident.status is IncidentStatus.RESOLVED
    assert incident.incident.version == 6
    events = await IncidentStore(database_session_factory).list_events(started.incident.incident.id)
    assert events[-1].event.actor.actor_id == "local_order_http_5xx_ratio_v1"


async def test_successful_samples_without_full_window_remain_running(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution, _ = await prepare_successful_execution(
        database_session_factory,
        suffix="short-window",
    )
    service = VerificationService(
        VerificationStore(database_session_factory),
        HealthyProbe(),
        clock=SequenceClock(
            [
                BASE_TIME + timedelta(seconds=8),
                BASE_TIME + timedelta(seconds=8),
                BASE_TIME + timedelta(seconds=18),
                BASE_TIME + timedelta(seconds=28),
            ]
        ),
    )
    run = (await service.create_run(execution.id, expected_incident_version=5)).run

    for number in range(1, 4):
        outcome = await service.sample(
            run.id,
            idempotency_key=f"short-window-sample-{number:03d}",
        )

    assert outcome.run.status is VerificationStatus.RUNNING
    assert outcome.run.successful_sample_count == 3


async def test_failed_threshold_is_audited_and_returns_to_investigation(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution, started = await prepare_successful_execution(
        database_session_factory,
        suffix="threshold-failed",
    )
    service = VerificationService(
        VerificationStore(database_session_factory),
        UnhealthyProbe(),
        clock=SequenceClock([BASE_TIME + timedelta(seconds=8), BASE_TIME + timedelta(seconds=9)]),
    )
    run = (await service.create_run(execution.id, expected_incident_version=5)).run

    outcome = await service.sample(run.id, idempotency_key="failed-sample-001")

    assert outcome.run.status is VerificationStatus.FAILED
    assert outcome.sample.status is VerificationSampleStatus.FAILED
    assert outcome.run.failure is not None
    assert outcome.run.failure.error_code == "recovery_threshold_not_met"
    incident = await IncidentStore(database_session_factory).get_incident(
        started.incident.incident.id
    )
    assert incident.incident.status is IncidentStatus.INVESTIGATING
    assert incident.incident.version == 6


async def test_probe_error_is_audited_and_returns_to_investigation(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution, started = await prepare_successful_execution(
        database_session_factory,
        suffix="probe-error",
    )
    service = VerificationService(
        VerificationStore(database_session_factory),
        BrokenProbe(),
        clock=SequenceClock([BASE_TIME + timedelta(seconds=8), BASE_TIME + timedelta(seconds=9)]),
    )
    run = (await service.create_run(execution.id, expected_incident_version=5)).run

    outcome = await service.sample(run.id, idempotency_key="error-sample-001")

    assert outcome.run.status is VerificationStatus.FAILED
    assert outcome.sample.status is VerificationSampleStatus.ERROR
    assert outcome.sample.failure is not None
    assert outcome.sample.failure.error_code == "verification_probe_unavailable"
    incident = await IncidentStore(database_session_factory).get_incident(
        started.incident.incident.id
    )
    assert incident.incident.status is IncidentStatus.INVESTIGATING


async def test_run_creation_is_idempotent_and_rejects_failed_execution(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution, _ = await prepare_successful_execution(
        database_session_factory,
        suffix="create-idempotent",
    )
    store = VerificationStore(database_session_factory)
    first = await store.create_for_execution(
        execution.id,
        expected_incident_version=5,
        occurred_at=BASE_TIME + timedelta(seconds=8),
    )
    duplicate = await store.create_for_execution(
        execution.id,
        expected_incident_version=999,
        occurred_at=BASE_TIME + timedelta(seconds=9),
    )
    assert first.created is True
    assert duplicate.created is False
    assert duplicate.run == first.run

    _, _, failed_step = await prepare_approved_step(
        database_session_factory,
        suffix="failed-execution",
    )
    failed = await OperationExecutionService(
        OperationExecutionStore(database_session_factory),
        FailingOperationAdapter(),
        clock=lambda: BASE_TIME + timedelta(seconds=7),
    ).execute_step(
        failed_step.id,
        idempotency_key="verify-operation-failed",
        expected_incident_version=4,
        occurred_at=BASE_TIME + timedelta(seconds=7),
    )

    with pytest.raises(OperationExecutionNotVerifiable) as captured:
        await store.create_for_execution(
            failed.execution.id,
            expected_incident_version=5,
            occurred_at=BASE_TIME + timedelta(seconds=8),
        )

    assert captured.value.reason_code == "execution_not_succeeded"


async def test_new_sample_after_terminal_run_is_rejected(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    execution, _ = await prepare_successful_execution(
        database_session_factory,
        suffix="terminal",
    )
    service = VerificationService(
        VerificationStore(database_session_factory),
        UnhealthyProbe(),
        clock=SequenceClock([BASE_TIME + timedelta(seconds=8), BASE_TIME + timedelta(seconds=9)]),
    )
    run = (await service.create_run(execution.id, expected_incident_version=5)).run
    await service.sample(run.id, idempotency_key="terminal-sample-001")

    with pytest.raises(VerificationRunCompleted):
        await service.sample(run.id, idempotency_key="terminal-sample-002")
