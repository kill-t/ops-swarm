"""从持久任务到 LangGraph checkpoint 恢复的多 Agent Worker 测试。"""

from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.incidents import (
    ActorType,
    AlertSubmission,
    IncidentActor,
    IncidentSeverity,
)
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.models import (
    EvidenceKind,
    InvestigationMode,
    InvestigationRunStatus,
    WorkItemKind,
    WorkItemStatus,
)
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.persistence import InvestigationStore, StartedInvestigation
from opsswarm.worker import InvestigationWorker, ProcessorContext, ProcessorOutcome
from opsswarm.worker.processor import LangGraphMultiAgentProcessor, ModeRoutingProcessor
from tests.checkpoint_fakes import (
    ConclusionSynthesizerStub,
    CountingDataSource,
    SpecialistAnalystStub,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
HUMAN_ACTOR = IncidentActor(actor_type=ActorType.HUMAN, actor_id="engineer-001")


class UnexpectedSingleAgentProcessor:
    """多 Agent 任务如果被错误路由到单 Agent，立即使测试失败。"""

    async def process(self, context: ProcessorContext) -> ProcessorOutcome:
        del context
        raise AssertionError("多 Agent 任务被错误路由到单 Agent")


def clock_from(values: Iterator[datetime]) -> datetime:
    return next(values)


async def start_multi_agent_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[InvestigationStore, StartedInvestigation]:
    incident = await IncidentStore(session_factory).ingest_alert(
        AlertSubmission(
            source="fault-simulator",
            external_event_id="multi-worker-alert-001",
            fingerprint="order-service:http-500:multi-worker",
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
        reason="启动多 Agent 调查",
        mode=InvestigationMode.MULTI_AGENT,
    )
    return store, started


async def test_multi_agent_worker_retries_from_existing_checkpoint(
    database_session_factory: async_sessionmaker[AsyncSession],
    checkpoint_database_url: str,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await start_multi_agent_investigation(database_session_factory)
    first_data_source = CountingDataSource()

    async with open_checkpoint_saver(checkpoint_database_url) as first_saver:
        first_processor = LangGraphMultiAgentProcessor(
            investigator=LangGraphParallelInvestigator(
                data_source=first_data_source,
                analyst=SpecialistAnalystStub(fail_logs=True),
                synthesizer=ConclusionSynthesizerStub(),
                checkpointer=first_saver,
            ),
            model_name="fake-multi-model",
        )
        first_times = iter(
            [
                BASE_TIME + timedelta(seconds=2),
                BASE_TIME + timedelta(seconds=3),
            ]
        )
        first_worker = InvestigationWorker(
            store=store,
            processor=ModeRoutingProcessor(
                single_agent=UnexpectedSingleAgentProcessor(),
                multi_agent=first_processor,
            ),
            worker_id="multi-worker-first",
            clock=lambda: clock_from(first_times),
            base_retry_delay=timedelta(0),
            max_retry_delay=timedelta(0),
        )
        assert await first_worker.run_once() is True

    first_work = await store.get_work_item(started.work_item.id)
    first_run = await store.get_run(started.run.id)
    first_board = await store.get_evidence_board(started.run.id)
    assert first_work.status is WorkItemStatus.PENDING
    assert first_work.attempt_count == 1
    assert first_run.status is InvestigationRunStatus.QUEUED
    assert first_board.conclusion is None
    assert first_board.evidence_items == ()
    assert first_board.specialist_reports == ()
    assert first_data_source.calls == {
        EvidenceKind.LOG: 1,
        EvidenceKind.METRIC: 1,
        EvidenceKind.CHANGE: 1,
    }

    recovered_data_source = CountingDataSource()
    async with open_checkpoint_saver(checkpoint_database_url) as second_saver:
        recovered_processor = LangGraphMultiAgentProcessor(
            investigator=LangGraphParallelInvestigator(
                data_source=recovered_data_source,
                analyst=SpecialistAnalystStub(),
                synthesizer=ConclusionSynthesizerStub(),
                checkpointer=second_saver,
            ),
            model_name="fake-multi-model",
        )
        recovery_times = iter(
            [
                BASE_TIME + timedelta(seconds=4),
                BASE_TIME + timedelta(seconds=5),
            ]
        )
        recovery_worker = InvestigationWorker(
            store=store,
            processor=ModeRoutingProcessor(
                single_agent=UnexpectedSingleAgentProcessor(),
                multi_agent=recovered_processor,
            ),
            worker_id="multi-worker-recovery",
            clock=lambda: clock_from(recovery_times),
            base_retry_delay=timedelta(0),
            max_retry_delay=timedelta(0),
        )
        assert await recovery_worker.run_once() is True

    completed_run = await store.get_run(started.run.id)
    completed_work = await store.get_work_item(started.work_item.id)
    assert recovered_data_source.calls == {
        EvidenceKind.LOG: 1,
        EvidenceKind.METRIC: 0,
        EvidenceKind.CHANGE: 0,
    }
    assert completed_run.mode is InvestigationMode.MULTI_AGENT
    assert completed_run.graph_thread_id == started.run.graph_thread_id
    assert completed_run.status is InvestigationRunStatus.SUCCEEDED
    assert completed_run.model_name == "fake-multi-model"
    assert completed_run.result is not None and completed_run.result.tool_call_count == 3
    assert completed_work.kind is WorkItemKind.RUN_MULTI_AGENT_INVESTIGATION
    assert completed_work.status is WorkItemStatus.COMPLETED
    assert completed_work.attempt_count == 2
    board = await store.get_evidence_board(started.run.id)
    assert board.conclusion == completed_run.result.conclusion
    assert {item.evidence_id for item in board.evidence_items} == {
        "log-order-runtime-error",
        "metric-order-error-spike",
        "change-order-release-rc1",
    }
    assert tuple(report.specialist for report in board.specialist_reports) == (
        EvidenceKind.CHANGE,
        EvidenceKind.LOG,
        EvidenceKind.METRIC,
    )
    assert {report.specialist: report.evidence_ids for report in board.specialist_reports} == {
        EvidenceKind.CHANGE: ("change-order-release-rc1",),
        EvidenceKind.LOG: ("log-order-runtime-error",),
        EvidenceKind.METRIC: ("metric-order-error-spike",),
    }
