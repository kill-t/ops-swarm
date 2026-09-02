"""白名单执行租约、幂等和 Incident 迁移的真实 PostgreSQL 测试。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval import ApprovalDecision
from opsswarm.execution import (
    ExecutionLeaseLost,
    ExecutionStatus,
    OperationAdapterFailure,
    OperationNotAuthorized,
    OperationResult,
)
from opsswarm.execution.persistence import OperationExecutionStore
from opsswarm.execution.persistence.tables import OperationExecutionRow
from opsswarm.execution.service import OperationExecutionService
from opsswarm.incidents import IncidentStatus
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.persistence import StartedInvestigation
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.planning.persistence import StoredActionPlan, StoredActionStep
from tests.integration.test_approval_request_store import (
    HUMAN_ACTOR,
    prepare_pending_request,
)
from tests.integration.test_investigation_store import BASE_TIME

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


class SuccessfulAdapter:
    def __init__(self) -> None:
        self.call_count = 0

    async def execute(self, step: StoredActionStep) -> OperationResult:
        self.call_count += 1
        return OperationResult(
            output={
                "fault": "release_500",
                "enabled": False,
                "service": step.target_name,
            }
        )


class FailingAdapter:
    async def execute(self, step: StoredActionStep) -> OperationResult:
        del step
        raise OperationAdapterFailure(
            error_code="fault_lab_unavailable",
            message="故障演练服务不可用",
        )


class BlockingAdapter(SuccessfulAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def execute(self, step: StoredActionStep) -> OperationResult:
        self.started.set()
        await self.release.wait()
        return await super().execute(step)


async def prepare_approved_step(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    suffix: str,
) -> tuple[StartedInvestigation, StoredActionPlan, StoredActionStep]:
    _, started, plan, approval_store, request = await prepare_pending_request(
        session_factory,
        suffix=suffix,
    )
    await approval_store.decide(
        request.id,
        decision=ApprovalDecision.APPROVE,
        expected_incident_version=3,
        occurred_at=BASE_TIME + timedelta(seconds=6),
        actor=HUMAN_ACTOR,
        reason="批准本地回滚演练",
    )
    return started, plan, plan.steps[0]


async def test_candidate_queue_returns_exact_approved_plan_and_removes_success(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    started, plan, step = await prepare_approved_step(
        database_session_factory,
        suffix="execution-candidate",
    )
    store = OperationExecutionStore(database_session_factory)

    page = await store.list_candidates(limit=10)

    assert page.total == 1
    assert len(page.items) == 1
    candidate = page.items[0]
    assert candidate.incident.id == started.incident.incident.id
    assert candidate.incident.status is IncidentStatus.MITIGATING
    assert candidate.incident.version == 4
    assert candidate.action_plan.id == plan.id
    assert candidate.action_plan.status.value == "approved"
    assert candidate.action_plan.steps == plan.steps
    assert candidate.approval_request.action_plan_id == plan.id
    assert candidate.approval_request.decided_by_id == HUMAN_ACTOR.actor_id
    assert candidate.active_executions == ()

    await OperationExecutionService(
        store,
        SuccessfulAdapter(),
        clock=lambda: BASE_TIME + timedelta(seconds=8),
    ).execute_step(
        step.id,
        idempotency_key="exec-candidate-001",
        expected_incident_version=4,
        occurred_at=BASE_TIME + timedelta(seconds=7),
    )

    completed_page = await store.list_candidates(limit=10)
    assert completed_page.total == 0
    assert completed_page.items == ()


async def test_candidate_queue_exposes_only_active_execution(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, plan, step = await prepare_approved_step(
        database_session_factory,
        suffix="execution-candidate-running",
    )
    store = OperationExecutionStore(database_session_factory)
    claimed = await store.claim(
        step.id,
        idempotency_key="exec-candidate-running-001",
        expected_incident_version=4,
        occurred_at=BASE_TIME + timedelta(seconds=7),
        lease_duration=timedelta(seconds=15),
    )

    page = await store.list_candidates(limit=10)

    assert page.total == 1
    assert page.items[0].action_plan.id == plan.id
    assert page.items[0].active_executions == (claimed.execution,)


@pytest.mark.parametrize("limit", [0, 101, True])
async def test_candidate_queue_rejects_unbounded_limit(
    database_session_factory: async_sessionmaker[AsyncSession],
    limit: object,
) -> None:
    with pytest.raises(ValueError, match="limit 必须在 1 到 100 之间"):
        await OperationExecutionStore(database_session_factory).list_candidates(  # type: ignore[arg-type]
            limit=limit
        )


async def test_old_approved_plan_cannot_execute_after_newer_investigation(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    started, _, step = await prepare_approved_step(
        database_session_factory,
        suffix="execution-old-investigation",
    )
    newer_time = BASE_TIME + timedelta(seconds=7)
    async with database_session_factory() as session, session.begin():
        session.add(
            InvestigationRunRow(
                id=uuid4(),
                incident_id=started.run.incident_id,
                mode="single_agent",
                data_source_mode="frozen",
                status="failed",
                prompt_version="latest-investigation-test-v1",
                graph_thread_id=None,
                model_name=None,
                result=None,
                metrics=None,
                last_error={
                    "error_code": "newer_investigation_failed",
                    "message": "用于验证旧批准计划不能再次执行",
                },
                created_at=newer_time,
                started_at=newer_time,
                completed_at=newer_time,
                updated_at=newer_time,
            )
        )

    store = OperationExecutionStore(database_session_factory)
    assert (await store.list_candidates(limit=10)).total == 0
    with pytest.raises(OperationNotAuthorized) as captured:
        await OperationExecutionService(store, SuccessfulAdapter()).execute_step(
            step.id,
            idempotency_key="exec-old-investigation-001",
            expected_incident_version=4,
            occurred_at=BASE_TIME + timedelta(seconds=8),
        )

    assert captured.value.reason_code == "plan_not_latest_investigation"


async def test_success_is_persisted_and_duplicate_key_does_not_repeat_adapter(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    started, _, step = await prepare_approved_step(
        database_session_factory,
        suffix="execution-success",
    )
    adapter = SuccessfulAdapter()
    service = OperationExecutionService(
        OperationExecutionStore(database_session_factory),
        adapter,
        clock=lambda: BASE_TIME + timedelta(seconds=7),
    )

    first = await service.execute_step(
        step.id,
        idempotency_key="exec-success-001",
        expected_incident_version=4,
        occurred_at=BASE_TIME + timedelta(seconds=7),
    )
    duplicate = await service.execute_step(
        step.id,
        idempotency_key="exec-success-001",
        expected_incident_version=4,
        occurred_at=BASE_TIME + timedelta(seconds=8),
    )

    assert first.created is True
    assert first.execution_performed is True
    assert first.execution.status is ExecutionStatus.SUCCEEDED
    assert duplicate.created is False
    assert duplicate.execution_performed is False
    assert duplicate.execution == first.execution
    assert adapter.call_count == 1
    incident_store = IncidentStore(database_session_factory)
    incident = await incident_store.get_incident(started.incident.incident.id)
    assert incident.incident.status is IncidentStatus.VERIFYING
    assert incident.incident.version == 5
    events = await incident_store.list_events(started.incident.incident.id)
    assert events[-1].event.actor.actor_id == "local_fault_lab_release_500_v1"
    assert len(events) == 5


async def test_adapter_failure_is_audited_and_returns_to_investigation(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    started, _, step = await prepare_approved_step(
        database_session_factory,
        suffix="execution-failure",
    )
    service = OperationExecutionService(
        OperationExecutionStore(database_session_factory),
        FailingAdapter(),
        clock=lambda: BASE_TIME + timedelta(seconds=7),
    )

    outcome = await service.execute_step(
        step.id,
        idempotency_key="exec-failure-001",
        expected_incident_version=4,
        occurred_at=BASE_TIME + timedelta(seconds=7),
    )

    assert outcome.execution.status is ExecutionStatus.FAILED
    assert outcome.execution.failure is not None
    assert outcome.execution.failure.error_code == "fault_lab_unavailable"
    incident = await IncidentStore(database_session_factory).get_incident(
        started.incident.incident.id
    )
    assert incident.incident.status is IncidentStatus.INVESTIGATING
    assert incident.incident.version == 5


async def test_same_key_concurrency_exposes_one_running_record_and_one_external_call(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, step = await prepare_approved_step(
        database_session_factory,
        suffix="execution-concurrent",
    )
    adapter = BlockingAdapter()
    service = OperationExecutionService(
        OperationExecutionStore(database_session_factory),
        adapter,
        clock=lambda: BASE_TIME + timedelta(seconds=8),
    )
    first_task = asyncio.create_task(
        service.execute_step(
            step.id,
            idempotency_key="exec-concurrent-001",
            expected_incident_version=4,
            occurred_at=BASE_TIME + timedelta(seconds=7),
        )
    )
    await adapter.started.wait()

    concurrent = await service.execute_step(
        step.id,
        idempotency_key="exec-concurrent-001",
        expected_incident_version=4,
        occurred_at=BASE_TIME + timedelta(seconds=8),
    )
    adapter.release.set()
    first = await first_task

    assert concurrent.execution.status is ExecutionStatus.RUNNING
    assert concurrent.execution_performed is False
    assert first.execution.status is ExecutionStatus.SUCCEEDED
    assert adapter.call_count == 1
    async with database_session_factory() as session:
        count = await session.scalar(select(func.count()).select_from(OperationExecutionRow))
    assert count == 1


async def test_expired_lease_can_be_reclaimed_with_same_key(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, step = await prepare_approved_step(
        database_session_factory,
        suffix="execution-recovery",
    )
    store = OperationExecutionStore(database_session_factory)
    abandoned = await store.claim(
        step.id,
        idempotency_key="exec-recovery-001",
        expected_incident_version=4,
        occurred_at=BASE_TIME + timedelta(seconds=7),
        lease_duration=timedelta(seconds=1),
    )
    adapter = SuccessfulAdapter()
    service = OperationExecutionService(
        store,
        adapter,
        clock=lambda: BASE_TIME + timedelta(seconds=9),
    )

    recovered = await service.execute_step(
        step.id,
        idempotency_key="exec-recovery-001",
        expected_incident_version=4,
        occurred_at=BASE_TIME + timedelta(seconds=9),
    )

    assert abandoned.execution.status is ExecutionStatus.RUNNING
    assert recovered.created is False
    assert recovered.execution_performed is True
    assert recovered.execution.status is ExecutionStatus.SUCCEEDED
    assert recovered.execution.attempt_count == 2
    assert adapter.call_count == 1


async def test_pending_approval_cannot_claim_execution(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, _, plan, _, _ = await prepare_pending_request(
        database_session_factory,
        suffix="execution-unauthorized",
    )
    service = OperationExecutionService(
        OperationExecutionStore(database_session_factory),
        SuccessfulAdapter(),
    )

    with pytest.raises(OperationNotAuthorized) as captured:
        await service.execute_step(
            plan.steps[0].id,
            idempotency_key="exec-unauthorized-001",
            expected_incident_version=3,
            occurred_at=BASE_TIME + timedelta(seconds=7),
        )

    assert captured.value.reason_code == "incident_not_mitigating"
    assert captured.value.step_id == plan.steps[0].id


async def test_completion_after_lease_expiration_cannot_commit_terminal_state(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    started, _, step = await prepare_approved_step(
        database_session_factory,
        suffix="execution-lease-expired",
    )
    service = OperationExecutionService(
        OperationExecutionStore(database_session_factory),
        SuccessfulAdapter(),
        clock=lambda: BASE_TIME + timedelta(seconds=23),
    )

    with pytest.raises(ExecutionLeaseLost):
        await service.execute_step(
            step.id,
            idempotency_key="exec-lease-expired-001",
            expected_incident_version=4,
            occurred_at=BASE_TIME + timedelta(seconds=7),
        )

    incident = await IncidentStore(database_session_factory).get_incident(
        started.incident.incident.id
    )
    assert incident.incident.status is IncidentStatus.MITIGATING
