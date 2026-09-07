"""验证固定 v2 图、业务任务与独立 checkpoint 的恢复边界。"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.models import EvidenceKind, WorkItemError
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource
from opsswarm.investigation.tasks.artifact_tables import AgentTaskResultRow
from opsswarm.investigation.tasks.contracts import TaskOutcome
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence_tables import AgentTaskEvidenceRow
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.processor import LangGraphMultiAgentProcessor
from opsswarm.worker.runner import InvestigationWorker, ProcessorContext
from opsswarm.worker.task_graph import TaskGraphFactory
from tests.checkpoint_fakes import (
    ConclusionSynthesizerStub,
    CountingDataSource,
    SpecialistAnalystStub,
)
from tests.task_facts_helpers import BASE_TIME, TaskClock, claimed_run, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def context(claim):
    return ProcessorContext(
        request=claim.request,
        mode=claim.run.mode,
        data_source_mode=claim.run.data_source_mode,
        work_item_kind=claim.work_item.kind,
        graph_thread_id=claim.run.graph_thread_id,
        workflow_version=claim.run.workflow_version,
        investigation_run_id=claim.run.id,
        lease_token=claim.work_item.lease_token,
        call_budget=claim.run.call_budget,
        model_budget=claim.run.model_budget,
        evidence_policy_version=claim.run.evidence_policy_version,
    )


def factory(db, saver, source, clock, analyst=None):
    return TaskGraphFactory(
        session_factory=db,
        checkpointer=saver,
        data_source=source,
        clock=clock,
        analyst=analyst or SpecialistAnalystStub(),
        synthesizer=ConclusionSynthesizerStub(),
    )


class FailLogsAfterCommittedPeers(SpecialistAnalystStub):
    def __init__(self, queries, run_id):
        super().__init__()
        self.queries = queries
        self.run_id = run_id

    async def analyze(self, *, specialist, request, evidence):
        if specialist is EvidenceKind.LOG:
            async with asyncio.timeout(10):
                while True:
                    snapshot = await self.queries.get_snapshot(self.run_id)
                    if sum(t.status == "succeeded" for t in snapshot.rounds[0].tasks) == 2:
                        raise RuntimeError("模拟日志分析失败")
                    await asyncio.sleep(0.01)
        return await super().analyze(specialist=specialist, request=request, evidence=evidence)


@pytest.mark.parametrize("fail_logs", [False, True])
async def test_worker_persists_stage_results_and_resumes_failed_specialist(
    database_session_factory,
    checkpoint_database_url,
    fail_logs,
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(
        database_session_factory, workflow_version=WorkflowVersion.V2
    )
    queries = CollaborationQueries(database_session_factory)
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    sources = []
    for index in range(2 if fail_logs else 1):
        source = CountingDataSource()
        sources.append(source)
        analyst = (
            FailLogsAfterCommittedPeers(queries, started.run.id)
            if fail_logs and index == 0
            else SpecialistAnalystStub()
        )
        async with open_checkpoint_saver(checkpoint_database_url) as saver:
            processor = LangGraphMultiAgentProcessor(
                investigator=LangGraphParallelInvestigator(
                    data_source=source,
                    analyst=SpecialistAnalystStub(),
                    synthesizer=ConclusionSynthesizerStub(),
                    checkpointer=saver,
                ),
                task_investigator_factory=factory(
                    database_session_factory, saver, source, clock, analyst
                ),
                model_name="fake-v2-model",
            )
            worker = InvestigationWorker(
                store=store,
                processor=processor,
                worker_id=f"v2-worker-{index}",
                clock=clock,
                base_retry_delay=timedelta(0),
                max_retry_delay=timedelta(0),
            )
            assert await worker.run_once()
        if fail_logs and index == 0:
            assert (await store.get_run(started.run.id)).status == "queued"
            assert (await store.get_evidence_board(started.run.id)).evidence_items == ()
            snapshot = await queries.get_snapshot(started.run.id)
            assert sum(t.status == "succeeded" for t in snapshot.rounds[0].tasks) == 2
            for task in snapshot.rounds[0].tasks:
                result = await queries.get_result(started.run.id, task.id)
                assert (result is not None) == (task.status == "succeeded")
        clock.current += timedelta(seconds=1)
    assert (await store.get_run(started.run.id)).status == "succeeded"
    snapshot = await queries.get_snapshot(started.run.id)
    assert len(snapshot.rounds) == 1 and snapshot.rounds[0].status == "completed"
    assert all(t.status == "succeeded" for t in snapshot.rounds[0].tasks)
    assert [t.attempt_count for t in snapshot.rounds[0].tasks] == (
        [1, 2, 1] if fail_logs else [1, 1, 1]
    )
    if fail_logs:
        assert sources[1].calls == {
            EvidenceKind.LOG: 0,
            EvidenceKind.METRIC: 0,
            EvidenceKind.CHANGE: 0,
        }
    events = await queries.list_events(started.run.id)
    assert [e.cursor for e in events] == list(range(1, len(events) + 1))
    assert sum(e.event_type == "task.succeeded" for e in events) == 3
    assert events[-1].event_type == "round.completed"


async def test_business_commit_before_checkpoint_reuses_result(
    database_session_factory,
    checkpoint_database_url,
    monkeypatch,
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, claim = await claimed_run(database_session_factory, workflow_version=WorkflowVersion.V2)
    clock = TaskClock()
    queries = CollaborationQueries(database_session_factory)
    original_finish = InvestigationTaskStore.finish_attempt

    async def fail_after_commit(self, *args, **kwargs):
        record = await original_finish(self, *args, **kwargs)
        if (
            kwargs.get("result") is not None
            and kwargs["result"].report.specialist is EvidenceKind.LOG
        ):
            raise RuntimeError("业务提交后、图节点返回前中断")
        return record

    async with open_checkpoint_saver(checkpoint_database_url) as saver:
        graph = factory(database_session_factory, saver, CountingDataSource(), clock)(
            context(claim)
        )
        with monkeypatch.context() as patch:
            patch.setattr(InvestigationTaskStore, "finish_attempt", fail_after_commit)
            with pytest.raises(RuntimeError, match="业务提交后"):
                await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        snapshot = await queries.get_snapshot(claim.run.id)
        log = next(t for t in snapshot.rounds[0].tasks if t.assignment.role is EvidenceKind.LOG)
        assert log.status == "succeeded" and await queries.get_result(claim.run.id, log.id)
        await parent.fail(
            claim.work_item.id,
            lease_token=claim.work_item.lease_token,
            occurred_at=clock(),
            error=WorkItemError(error_code="test_crash", message="模拟中断"),
            retry_delay=timedelta(0),
        )
        clock.current += timedelta(seconds=1)
        next_claim = await parent.claim_next(
            worker_id="recovery", occurred_at=clock(), lease_duration=timedelta(seconds=30)
        )
        recovered_source = CountingDataSource()
        recovered = factory(database_session_factory, saver, recovered_source, clock)(
            context(next_claim)
        )
        outcome = await recovered.resume(thread_id=claim.run.graph_thread_id)
    assert recovered_source.calls[EvidenceKind.LOG] == 0
    assert outcome.result.tool_call_count == 3
    current = await queries.get_snapshot(claim.run.id)
    assert next(t for t in current.rounds[0].tasks if t.id == log.id).attempt_count == 1
    assert (
        sum(
            e.event_type == "task.succeeded" and e.task_id == log.id
            for e in await queries.list_events(claim.run.id)
        )
        == 1
    )


@pytest.mark.parametrize("missing", ["result", "evidence"])
async def test_checkpoint_version_mismatch_and_missing_business_result_fail_closed(
    database_session_factory,
    checkpoint_database_url,
    missing,
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    _, claim = await claimed_run(database_session_factory, workflow_version=WorkflowVersion.V2)
    async with open_checkpoint_saver(checkpoint_database_url) as saver:
        graph = factory(database_session_factory, saver, CountingDataSource(), TaskClock())(
            context(claim)
        )
        await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        legacy = LangGraphParallelInvestigator(
            data_source=CountingDataSource(),
            analyst=SpecialistAnalystStub(),
            synthesizer=ConclusionSynthesizerStub(),
            checkpointer=saver,
        )
        with pytest.raises(RuntimeError, match="版本不兼容"):
            await legacy.resume(thread_id=claim.run.graph_thread_id)
        table = AgentTaskResultRow if missing == "result" else AgentTaskEvidenceRow
        async with database_session_factory() as session, session.begin():
            row = await session.scalar(
                select(table).where(table.investigation_run_id == claim.run.id)
            )
            await session.delete(row)
        message = "必须提交三个专业结果" if missing == "result" else "缺少已提交的取证快照"
        with pytest.raises(TaskFactConflict, match=message):
            await graph.resume(thread_id=claim.run.graph_thread_id)


@pytest.mark.parametrize("expired", [False, True])
async def test_parent_terminal_failure_settles_children(database_session_factory, expired):
    store, started = await started_run(database_session_factory, max_attempts=1)
    claim = await store.claim_next(
        worker_id="one",
        occurred_at=BASE_TIME + timedelta(seconds=2),
        lease_duration=timedelta(seconds=30),
    )
    clock = TaskClock()
    tasks = InvestigationTaskStore(database_session_factory, clock=clock)
    from tests.task_facts_helpers import assignment

    await tasks.create_round(
        claim.run.id, lease_token=claim.work_item.lease_token, assignment=assignment(count=3)
    )
    queries = CollaborationQueries(database_session_factory)
    first = (await queries.get_snapshot(claim.run.id)).rounds[0].tasks[0]
    attempt = await tasks.begin_attempt(
        claim.run.id, first.id, lease_token=claim.work_item.lease_token, expected_version=1
    )
    if expired:
        clock.current = BASE_TIME + timedelta(seconds=33)
        assert (
            await store.claim_next(
                worker_id="takeover", occurred_at=clock(), lease_duration=timedelta(seconds=30)
            )
            is None
        )
    else:
        await store.fail(
            claim.work_item.id,
            lease_token=claim.work_item.lease_token,
            occurred_at=clock(),
            error=WorkItemError(error_code="test_failure", message="模拟失败"),
            retry_delay=timedelta(0),
        )
    snapshot = await queries.get_snapshot(claim.run.id)
    assert snapshot.rounds[0].status == "failed"
    assert all(t.status == "failed" for t in snapshot.rounds[0].tasks)
    assert snapshot.rounds[0].tasks[0].attempts[0].status == (
        "lease_expired" if expired else "failed"
    )
    assert (await store.get_run(started.run.id)).status == "failed"
    with pytest.raises(TaskFactConflict):
        await tasks.finish_attempt(
            claim.run.id,
            first.id,
            attempt.attempt.id,
            lease_token=claim.work_item.lease_token,
            outcome=TaskOutcome(status="succeeded", summary="迟到结果"),
        )


async def test_v2_rejects_v1_checkpoint_before_creating_task_facts(
    database_session_factory, checkpoint_database_url
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    _, claim = await claimed_run(database_session_factory, workflow_version=WorkflowVersion.V2)
    async with open_checkpoint_saver(checkpoint_database_url) as saver:
        legacy = LangGraphParallelInvestigator(
            data_source=CountingDataSource(),
            analyst=SpecialistAnalystStub(),
            synthesizer=ConclusionSynthesizerStub(),
            checkpointer=saver,
        )
        await legacy.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        graph = factory(database_session_factory, saver, CountingDataSource(), TaskClock())(
            context(claim)
        )
        with pytest.raises(RuntimeError, match="版本不兼容"):
            await graph.resume(thread_id=claim.run.graph_thread_id)
    assert (
        await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    ).rounds == ()


async def test_changed_frozen_snapshot_cannot_resume_original_tasks(
    database_session_factory, checkpoint_database_url
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    _, claim = await claimed_run(database_session_factory, workflow_version=WorkflowVersion.V2)
    async with open_checkpoint_saver(checkpoint_database_url) as saver:
        graph = factory(database_session_factory, saver, CountingDataSource(), TaskClock())(
            context(claim)
        )
        await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        changed_source = FrozenScenarioDataSource(
            RELEASE_500_SCENARIO.model_copy(update={"scenario_id": "another-version"})
        )
        recovered = factory(database_session_factory, saver, changed_source, TaskClock())(
            context(claim)
        )
        with pytest.raises(TaskFactConflict, match="另一份分派"):
            await recovered.resume(thread_id=claim.run.graph_thread_id)


async def test_input_only_checkpoint_can_resume(database_session_factory, checkpoint_database_url):
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, claim = await claimed_run(database_session_factory, workflow_version=WorkflowVersion.V2)
    async with open_checkpoint_saver(checkpoint_database_url) as saver:
        graph = factory(database_session_factory, saver, CountingDataSource(), TaskClock())(
            context(claim)
        )
        await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
        config = {"configurable": {"thread_id": claim.run.graph_thread_id, "checkpoint_ns": ""}}
        checkpoints = [item async for item in saver.alist(config)]
        initial = next(item for item in checkpoints if item.metadata["step"] == -1)
        assert "request" not in initial.checkpoint["channel_values"]
        await saver.adelete_thread(claim.run.graph_thread_id)
        await saver.aput(
            config, initial.checkpoint, initial.metadata, initial.checkpoint["channel_versions"]
        )
        await parent.fail(
            claim.work_item.id,
            lease_token=claim.work_item.lease_token,
            occurred_at=BASE_TIME + timedelta(seconds=3),
            error=WorkItemError(error_code="crash", message="模拟恢复到初始快照"),
            retry_delay=timedelta(0),
        )
        replacement = await parent.claim_next(
            worker_id="input-recovery",
            occurred_at=BASE_TIME + timedelta(seconds=4),
            lease_duration=timedelta(seconds=30),
        )
        recovered_source = CountingDataSource()
        recovered = factory(
            database_session_factory,
            saver,
            recovered_source,
            TaskClock(BASE_TIME + timedelta(seconds=4)),
        )(context(replacement))
        result = await recovered.resume(thread_id=claim.run.graph_thread_id)
    assert result.result.tool_call_count == 3
    assert all(value == 0 for value in recovered_source.calls.values())
