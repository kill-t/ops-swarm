"""调查运行、租约队列与独立 Worker 的真实 PostgreSQL 测试。"""

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents import (
    ActorType,
    AlertSubmission,
    IncidentActor,
    IncidentSeverity,
    IncidentStatus,
)
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.errors import WorkItemLeaseLost
from opsswarm.investigation.models import (
    InvestigationConclusion,
    InvestigationMetrics,
    InvestigationRequest,
    InvestigationRunResult,
    InvestigationRunStatus,
    WorkItemError,
    WorkItemStatus,
)
from opsswarm.investigation.persistence import InvestigationStore, StartedInvestigation
from opsswarm.worker import InvestigationWorker, ProcessorOutcome

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
HUMAN_ACTOR = IncidentActor(actor_type=ActorType.HUMAN, actor_id="engineer-001")


def make_outcome() -> ProcessorOutcome:
    result = InvestigationRunResult(
        conclusion=InvestigationConclusion(
            root_cause="release_500_regression",
            confidence=0.96,
            summary="新版本发布与错误率上升时间一致。",
            evidence_ids=(
                "log-order-runtime-error",
                "metric-order-error-spike",
                "change-order-release-rc1",
            ),
            recommendation="rollback_release",
            requires_human_approval=True,
            safety_notes=("回滚必须经过人工审批。",),
        ),
        returned_evidence_ids=(
            "change-order-release-rc1",
            "log-order-runtime-error",
            "metric-order-error-spike",
        ),
        tool_call_count=3,
    )
    return ProcessorOutcome(
        result=result,
        metrics=InvestigationMetrics(
            top1_accuracy=1,
            evidence_recall=1,
            recommendation_accuracy=1,
            safety_violation_rate=0,
            tool_call_count=3,
        ),
        model_name="fake-tool-model",
    )


async def start_investigation(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str = "001",
    max_attempts: int = 3,
) -> tuple[InvestigationStore, StartedInvestigation]:
    incident_store = IncidentStore(session_factory)
    incident = await incident_store.ingest_alert(
        AlertSubmission(
            source="fault-simulator",
            external_event_id=f"alert-{suffix}",
            fingerprint=f"order-service:http-500:{suffix}",
            service_name="order-service",
            severity=IncidentSeverity.P1,
            title="订单接口大量返回 500",
            payload={"status_code": 500},
            occurred_at=BASE_TIME,
            ingested_at=BASE_TIME,
        )
    )
    store = InvestigationStore(session_factory)
    started = await store.start_investigation(
        incident.incident.id,
        expected_version=1,
        occurred_at=BASE_TIME + timedelta(seconds=1),
        actor=HUMAN_ACTOR,
        reason="开始自动调查",
        max_attempts=max_attempts,
    )
    return store, started


async def test_start_atomically_persists_incident_run_and_work_item(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    incident_store = IncidentStore(database_session_factory)
    store, started = await start_investigation(database_session_factory)

    persisted_incident = await incident_store.get_incident(started.incident.incident.id)
    persisted_run = await store.get_run(started.run.id)
    persisted_work = await store.get_work_item(started.work_item.id)

    assert persisted_incident.incident.status is IncidentStatus.INVESTIGATING
    assert persisted_incident.incident.version == 2
    assert persisted_run.status is InvestigationRunStatus.QUEUED
    assert persisted_work.status is WorkItemStatus.PENDING
    assert persisted_work.attempt_count == 0
    assert len(await incident_store.list_events(persisted_incident.incident.id)) == 2


async def test_task_insert_failure_rolls_back_incident_transition(
    database_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, existing = await start_investigation(database_session_factory, suffix="001")
    incident_store = IncidentStore(database_session_factory)
    second_incident = await incident_store.ingest_alert(
        AlertSubmission(
            source="fault-simulator",
            external_event_id="alert-002",
            fingerprint="order-service:http-500:002",
            service_name="order-service",
            severity=IncidentSeverity.P1,
            title="第二个订单事故",
            payload={"status_code": 500},
            occurred_at=BASE_TIME,
            ingested_at=BASE_TIME,
        )
    )
    monkeypatch.setattr(
        "opsswarm.investigation.persistence.repository.uuid4",
        lambda: existing.run.id,
    )
    store = InvestigationStore(database_session_factory)

    with pytest.raises(IntegrityError):
        await store.start_investigation(
            second_incident.incident.id,
            expected_version=1,
            occurred_at=BASE_TIME + timedelta(seconds=1),
            actor=HUMAN_ACTOR,
            reason="模拟任务写入失败",
        )

    persisted = await incident_store.get_incident(second_incident.incident.id)
    assert persisted.incident.status is IncidentStatus.OPEN
    assert persisted.incident.version == 1
    assert len(await incident_store.list_events(second_incident.incident.id)) == 1


async def test_concurrent_workers_only_claim_one_available_item(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, _ = await start_investigation(database_session_factory)

    claims = await asyncio.gather(
        *(
            store.claim_next(
                worker_id=f"worker-{number}",
                occurred_at=BASE_TIME + timedelta(seconds=2),
                lease_duration=timedelta(seconds=30),
            )
            for number in range(8)
        )
    )

    claimed = [item for item in claims if item is not None]
    assert len(claimed) == 1
    assert claimed[0].work_item.attempt_count == 1


async def test_expired_lease_is_reclaimed_and_stale_worker_cannot_complete(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, started = await start_investigation(database_session_factory)
    first = await store.claim_next(
        worker_id="worker-a",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=10),
    )
    assert first is not None
    second = await store.claim_next(
        worker_id="worker-b",
        occurred_at=BASE_TIME + timedelta(seconds=13),
        lease_duration=timedelta(seconds=10),
    )
    assert second is not None
    assert first.work_item.lease_token != second.work_item.lease_token
    assert second.work_item.attempt_count == 2
    outcome = make_outcome()

    with pytest.raises(WorkItemLeaseLost):
        await store.complete(
            first.work_item.id,
            lease_token=first.work_item.lease_token,
            occurred_at=BASE_TIME + timedelta(seconds=14),
            result=outcome.result,
            metrics=outcome.metrics,
            model_name=outcome.model_name,
        )

    completed = await store.complete(
        second.work_item.id,
        lease_token=second.work_item.lease_token,
        occurred_at=BASE_TIME + timedelta(seconds=14),
        result=outcome.result,
        metrics=outcome.metrics,
        model_name=outcome.model_name,
    )

    assert completed.id == started.run.id
    assert completed.status is InvestigationRunStatus.SUCCEEDED
    assert completed.result == outcome.result


async def test_failure_retries_after_delay_then_becomes_terminal(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, started = await start_investigation(database_session_factory, max_attempts=2)
    first = await store.claim_next(
        worker_id="worker-a",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
    )
    assert first is not None and first.work_item.lease_token is not None
    error = WorkItemError(error_code="model_timeout", message="模型调用超时")
    retrying = await store.fail(
        first.work_item.id,
        lease_token=first.work_item.lease_token,
        occurred_at=BASE_TIME + timedelta(seconds=3),
        error=error,
        retry_delay=timedelta(seconds=5),
    )

    assert retrying.status is WorkItemStatus.PENDING
    assert (
        await store.claim_next(
            worker_id="worker-b",
            occurred_at=BASE_TIME + timedelta(seconds=7),
            lease_duration=timedelta(seconds=30),
        )
        is None
    )
    second = await store.claim_next(
        worker_id="worker-b",
        occurred_at=BASE_TIME + timedelta(seconds=8),
        lease_duration=timedelta(seconds=30),
    )
    assert second is not None and second.work_item.lease_token is not None
    failed = await store.fail(
        second.work_item.id,
        lease_token=second.work_item.lease_token,
        occurred_at=BASE_TIME + timedelta(seconds=9),
        error=error,
        retry_delay=timedelta(seconds=5),
    )

    assert failed.status is WorkItemStatus.FAILED
    assert failed.completed_at == BASE_TIME + timedelta(seconds=9)
    run = await store.get_run(started.run.id)
    assert run.status is InvestigationRunStatus.FAILED
    assert run.last_error == error


async def test_expired_last_attempt_is_closed_as_terminal_failure(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, started = await start_investigation(database_session_factory, max_attempts=1)
    claimed = await store.claim_next(
        worker_id="worker-crashed",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=10),
    )
    assert claimed is not None

    reclaimed = await store.claim_next(
        worker_id="worker-recovery",
        occurred_at=BASE_TIME + timedelta(seconds=13),
        lease_duration=timedelta(seconds=10),
    )

    assert reclaimed is None
    work = await store.get_work_item(started.work_item.id)
    run = await store.get_run(started.run.id)
    assert work.status is WorkItemStatus.FAILED
    assert work.last_error is not None
    assert work.last_error.error_code == "lease_expired_after_max_attempts"
    assert run.status is InvestigationRunStatus.FAILED


class RecordingProcessor:
    """记录 Worker 传入的事故上下文并返回固定成功结果。"""

    def __init__(self) -> None:
        self.requests: list[InvestigationRequest] = []

    async def process(self, request: InvestigationRequest) -> ProcessorOutcome:
        self.requests.append(request)
        return make_outcome()


class FailingProcessor:
    """模拟可重试的模型超时。"""

    async def process(self, request: InvestigationRequest) -> ProcessorOutcome:
        del request
        raise TimeoutError("模型调用超时")


def clock_from(values: Iterator[datetime]) -> datetime:
    return next(values)


async def test_worker_processes_one_item_and_persists_structured_result(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, started = await start_investigation(database_session_factory)
    processor = RecordingProcessor()
    times = iter(
        [
            BASE_TIME + timedelta(seconds=2),
            BASE_TIME + timedelta(seconds=3),
        ]
    )
    worker = InvestigationWorker(
        store=store,
        processor=processor,
        worker_id="worker-success",
        clock=lambda: clock_from(times),
    )

    assert await worker.run_once() is True
    assert processor.requests[0].incident_id == started.incident.incident.id
    run = await store.get_run(started.run.id)
    work = await store.get_work_item(started.work_item.id)
    assert run.status is InvestigationRunStatus.SUCCEEDED
    assert run.model_name == "fake-tool-model"
    assert run.metrics is not None and run.metrics.top1_accuracy == 1
    assert work.status is WorkItemStatus.COMPLETED

    idle_times = iter([BASE_TIME + timedelta(seconds=4)])
    idle_worker = InvestigationWorker(
        store=store,
        processor=processor,
        worker_id="worker-idle",
        clock=lambda: clock_from(idle_times),
    )
    assert await idle_worker.run_once() is False


async def test_worker_failure_is_safely_summarized_and_requeued(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    store, started = await start_investigation(database_session_factory)
    times = iter(
        [
            BASE_TIME + timedelta(seconds=2),
            BASE_TIME + timedelta(seconds=3),
        ]
    )
    worker = InvestigationWorker(
        store=store,
        processor=FailingProcessor(),
        worker_id="worker-timeout",
        clock=lambda: clock_from(times),
    )

    assert await worker.run_once() is True
    work = await store.get_work_item(started.work_item.id)
    run = await store.get_run(started.run.id)
    assert work.status is WorkItemStatus.PENDING
    assert work.available_at == BASE_TIME + timedelta(seconds=8)
    assert work.last_error == WorkItemError(
        error_code="TimeoutError",
        message="调查处理器调用超时",
    )
    assert run.status is InvestigationRunStatus.QUEUED
