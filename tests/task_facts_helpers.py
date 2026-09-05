"""任务事实测试共用的真实运行、租约和可控时钟。"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents import ActorType, AlertSubmission, IncidentActor, IncidentSeverity
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.models import EvidenceKind, InvestigationMode
from opsswarm.investigation.persistence import (
    ClaimedInvestigation,
    InvestigationStore,
    StartedInvestigation,
)
from opsswarm.investigation.tasks.contracts import RoundAssignment, TaskAssignment
from opsswarm.investigation.workflow_versions import WorkflowVersion

BASE_TIME = datetime(2026, 9, 5, tzinfo=UTC)


@dataclass
class TaskClock:
    current: datetime = BASE_TIME + timedelta(seconds=3)

    def __call__(self) -> datetime:
        return self.current


async def claimed_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    workflow_version: WorkflowVersion = WorkflowVersion.V1,
) -> tuple[InvestigationStore, ClaimedInvestigation]:
    store, _ = await started_run(factory, workflow_version=workflow_version)
    claim = await store.claim_next(
        worker_id=str(uuid4()),
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
    )
    assert claim is not None
    return store, claim


async def started_run(
    factory: async_sessionmaker[AsyncSession],
    *,
    workflow_version: WorkflowVersion = WorkflowVersion.V1,
    max_attempts: int = 3,
) -> tuple[InvestigationStore, StartedInvestigation]:
    key = str(uuid4())
    incident = await IncidentStore(factory).ingest_alert(
        AlertSubmission(
            source="task-facts-test",
            external_event_id=key,
            fingerprint=key,
            service_name="order-service",
            severity=IncidentSeverity.P1,
            title="任务事实测试",
            payload={},
            occurred_at=BASE_TIME,
            ingested_at=BASE_TIME,
        )
    )
    store = InvestigationStore(factory)
    started = await store.start_investigation(
        incident.incident.id,
        expected_version=1,
        occurred_at=BASE_TIME + timedelta(seconds=1),
        actor=IncidentActor(actor_type=ActorType.HUMAN, actor_id="test-engineer"),
        reason="创建任务事实测试运行",
        mode=InvestigationMode.MULTI_AGENT,
        workflow_version=workflow_version,
        max_attempts=max_attempts,
    )
    return store, started


def assignment(*, round_number: int = 1, max_attempts: int = 3, count: int = 1) -> RoundAssignment:
    return RoundAssignment(
        round_number=round_number,
        goal="调查订单错误",
        tasks=tuple(
            TaskAssignment(
                task_key=role.value,
                role=role,
                goal="读取所属专业材料",
                service_name="order-service",
                window_start=BASE_TIME - timedelta(minutes=15),
                window_end=BASE_TIME,
                max_attempts=max_attempts,
            )
            for role in tuple(EvidenceKind)[:count]
        ),
    )
