"""现场 v2 在真实业务库、checkpoint 与异步适配器间恢复。"""

import asyncio
from datetime import timedelta
from threading import get_ident

import pytest
from sqlalchemy import update

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.github_changes import GitHubChangeSettings
from opsswarm.investigation.live import (
    LiveInvestigationSettings,
    create_live_investigation_data_source,
)
from opsswarm.investigation.models import EvidenceKind, InvestigationDataSourceMode
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.query_execution import ReadOnlyQueryExecutor
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot, evidence_hash
from opsswarm.investigation.tasks.evidence_store import TaskEvidenceStore
from opsswarm.investigation.tasks.evidence_tables import AgentTaskEvidenceRow
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.tables import AgentTaskRow
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.worker.processor import LangGraphMultiAgentProcessor
from opsswarm.worker.runner import InvestigationWorker
from opsswarm.worker.task_graph import TaskGraphFactory
from tests.checkpoint_fakes import ConclusionSynthesizerStub, SpecialistAnalystStub
from tests.integration.test_fixed_task_graph import FailLogsAfterCommittedPeers
from tests.live_task_helpers import LiveHttpFixture
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
LIVE = InvestigationDataSourceMode.LIVE


async def run_live_worker(db, url, store, clock, source, analyst=None):
    executor = ReadOnlyQueryExecutor()
    try:
        async with open_checkpoint_saver(url) as saver:
            factory = TaskGraphFactory(
                session_factory=db,
                checkpointer=saver,
                data_source=source,
                clock=clock,
                analyst=analyst or SpecialistAnalystStub(),
                synthesizer=ConclusionSynthesizerStub(),
                query_executor=executor,
            )
            processor = LangGraphMultiAgentProcessor(
                investigator=LangGraphParallelInvestigator(
                    data_source=source,
                    analyst=SpecialistAnalystStub(),
                    synthesizer=ConclusionSynthesizerStub(),
                    checkpointer=saver,
                    query_executor=executor,
                ),
                task_investigator_factory=factory,
                model_name="fake-live-task-model",
                data_source_mode=LIVE,
                evaluation_scenario=None,
            )
            return await InvestigationWorker(
                store=store,
                processor=processor,
                worker_id="live-v2-test",
                clock=clock,
                data_source_mode=LIVE,
                base_retry_delay=timedelta(0),
                max_retry_delay=timedelta(0),
            ).run_once()
    finally:
        executor.close()


@pytest.mark.parametrize("fail_model", [False, True])
async def test_live_worker_pins_scope_and_recovers_saved_evidence(
    database_session_factory, checkpoint_database_url, fail_model
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(
        db, workflow_version=WorkflowVersion.V2, data_source_mode=LIVE
    )
    cutoff = started.run.observation_cutoff_at
    assert cutoff == BASE_TIME + timedelta(seconds=1)
    clock = TaskClock(BASE_TIME + timedelta(hours=1))
    queries = CollaborationQueries(db)
    first = LiveHttpFixture(cutoff, clock, concurrent=True)
    analyst = FailLogsAfterCommittedPeers(queries, started.run.id) if fail_model else None
    assert await run_live_worker(db, checkpoint_database_url, store, clock, first.source, analyst)
    assert len(first.thread_ids) == 3 and get_ident() not in first.thread_ids
    first.assert_windows()
    tasks = (await queries.get_snapshot(started.run.id)).rounds[0].tasks
    saved = [await queries.get_evidence(started.run.id, t.id) for t in tasks]
    assert all(r.snapshot.data_source_mode == "live" for r in saved)
    assert all(r.snapshot.version == "task-evidence-live-mcp-log-v1" for r in saved)
    assert all(t.assignment.window_end == cutoff for t in tasks)
    if fail_model:
        assert (await store.get_run(started.run.id)).status == "queued"
        clock.current += timedelta(seconds=60)
        restarted = LiveHttpFixture(cutoff, clock, failure="log")
        assert await run_live_worker(db, checkpoint_database_url, store, clock, restarted.source)
        assert restarted.requests == []
        assert saved == [await queries.get_evidence(started.run.id, t.id) for t in tasks]
    run = await store.get_run(started.run.id)
    assert run.status == "succeeded" and run.metrics is None
    board = await store.get_evidence_board(run.id)
    assert {e.source for e in board.evidence_items} == {
        "loki-fault-lab",
        "prometheus-fault-lab",
        "change-simulator",
    }
    changes = [e for e in board.evidence_items if e.kind is EvidenceKind.CHANGE]
    deployment = next(e for e in changes if e.attributes["change_type"] == "deployment")
    assert deployment.attributes["runtime_artifact_status"] == "historical"
    budget = await CallBudgetQueries(db).get_snapshot(run.id)
    assert budget.reserved_tool_calls == 3
    assert budget.reserved_model_calls == (5 if fail_model else 4)


@pytest.mark.parametrize("committed", [False, True])
async def test_live_snapshot_commit_boundary_keeps_window_and_budget(
    database_session_factory, checkpoint_database_url, monkeypatch, committed
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(
        db, workflow_version=WorkflowVersion.V2, data_source_mode=LIVE
    )
    clock = TaskClock()
    queries = CollaborationQueries(db)
    original = TaskEvidenceStore.save

    async def interrupt(self, *args, **kwargs):
        if kwargs["evidence"][0].kind is not EvidenceKind.LOG:
            return await original(self, *args, **kwargs)
        async with asyncio.timeout(10):
            while True:
                tasks = (await queries.get_snapshot(started.run.id)).rounds[0].tasks
                if sum(t.status == "succeeded" for t in tasks) == 2:
                    break
                await asyncio.sleep(0.01)
        if committed:
            await original(self, *args, **kwargs)
        raise RuntimeError("现场材料提交边界中断")

    first = LiveHttpFixture(started.run.observation_cutoff_at, clock)
    with monkeypatch.context() as patch:
        patch.setattr(TaskEvidenceStore, "save", interrupt)
        assert await run_live_worker(db, checkpoint_database_url, store, clock, first.source)
    assert (await store.get_run(started.run.id)).status == "queued"
    tasks = (await queries.get_snapshot(started.run.id)).rounds[0].tasks
    log = next(t for t in tasks if t.assignment.role is EvidenceKind.LOG)
    before = await queries.get_evidence(started.run.id, log.id)
    assert (before is not None) == committed
    clock.current += timedelta(seconds=60)
    restarted = LiveHttpFixture(started.run.observation_cutoff_at, clock)
    assert await run_live_worker(db, checkpoint_database_url, store, clock, restarted.source)
    assert (await store.get_run(started.run.id)).status == "succeeded"
    first.assert_windows()
    restarted.assert_windows()
    assert len(restarted.requests) == (0 if committed else 1)
    after = await queries.get_evidence(started.run.id, log.id)
    assert (after == before) if committed else after.attempt_id != log.attempts[0].id
    budget = await CallBudgetQueries(db).get_snapshot(started.run.id)
    assert budget.reserved_tool_calls == (3 if committed else 4)
    assert budget.reserved_model_calls == 4
    events = await queries.list_events(started.run.id)
    assert sum(e.event_type == "task.evidence_saved" for e in events) == 3


@pytest.mark.parametrize("failed_role", ["log", "metric", "change"])
async def test_unavailable_live_source_never_creates_frozen_success(
    database_session_factory, checkpoint_database_url, failed_role
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(
        db, workflow_version=WorkflowVersion.V2, data_source_mode=LIVE, max_attempts=1
    )
    clock = TaskClock()
    fixture = LiveHttpFixture(started.run.observation_cutoff_at, clock, failure=failed_role)
    assert await run_live_worker(db, checkpoint_database_url, store, clock, fixture.source)
    run = await store.get_run(started.run.id)
    assert run.status == "failed" and run.result is None and run.metrics is None
    assert (await store.get_evidence_board(run.id)).evidence_items == ()
    queries = CollaborationQueries(db)
    tasks = (await queries.get_snapshot(run.id)).rounds[0].tasks
    failed = next(t for t in tasks if t.assignment.role.value == failed_role)
    assert await queries.get_evidence(run.id, failed.id) is None
    budget = await CallBudgetQueries(db).get_snapshot(run.id)
    assert any(c.kind == "tool" and c.status == "failed" for c in budget.calls)


async def test_github_configuration_fails_once_before_external_calls(
    database_session_factory, checkpoint_database_url, monkeypatch
):
    import httpx2

    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(
        db, workflow_version=WorkflowVersion.V2, data_source_mode=LIVE
    )

    def forbidden(*args, **kwargs):
        raise AssertionError("来源配置错误必须在外部请求前发现")

    monkeypatch.setattr(httpx2.Client, "send", forbidden)
    source = create_live_investigation_data_source(
        LiveInvestigationSettings(change_provider="simulator_and_github"),
        github_settings=GitHubChangeSettings(repository="example/ops-swarm"),
    )
    assert await run_live_worker(db, checkpoint_database_url, store, TaskClock(), source)
    run = await store.get_run(started.run.id)
    assert run.status == "failed" and run.last_error.error_code == "unsupported_live_task_source"
    assert not await run_live_worker(db, checkpoint_database_url, store, TaskClock(), source)
    assert (await CallBudgetQueries(db).get_snapshot(run.id)).calls == ()
    assert (await CollaborationQueries(db).get_snapshot(run.id)).rounds == ()


async def test_live_budget_stops_before_excess_queries(
    database_session_factory, checkpoint_database_url
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(
        db,
        workflow_version=WorkflowVersion.V2,
        data_source_mode=LIVE,
        call_budget=CallBudgetPolicy(max_tool_calls=1),
    )
    clock = TaskClock()
    fixture = LiveHttpFixture(started.run.observation_cutoff_at, clock)
    assert await run_live_worker(db, checkpoint_database_url, store, clock, fixture.source)
    run = await store.get_run(started.run.id)
    assert run.status == "failed"
    assert len({kind for kind, _ in fixture.requests}) <= 1
    budget = await CallBudgetQueries(db).get_snapshot(run.id)
    assert budget.reserved_tool_calls == 1
    assert not await run_live_worker(db, checkpoint_database_url, store, clock, fixture.source)


@pytest.mark.parametrize("wrong", ["mode", "scope"])
async def test_rehashed_material_cannot_change_pinned_run_scope(
    database_session_factory, checkpoint_database_url, wrong
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(
        db, workflow_version=WorkflowVersion.V2, data_source_mode=LIVE
    )
    clock = TaskClock()
    queries = CollaborationQueries(db)
    fixture = LiveHttpFixture(started.run.observation_cutoff_at, clock)
    assert await run_live_worker(
        db,
        checkpoint_database_url,
        store,
        clock,
        fixture.source,
        FailLogsAfterCommittedPeers(queries, started.run.id),
    )
    tasks = (await queries.get_snapshot(started.run.id)).rounds[0].tasks
    log = next(t for t in tasks if t.assignment.role is EvidenceKind.LOG)
    record = await queries.get_evidence(started.run.id, log.id)
    body = record.snapshot.model_dump(mode="json")
    if wrong == "mode":
        body.update(version="task-evidence-v1", data_source_mode="frozen")
    else:
        body["evidence_snapshot_id"] = "another-run-scope"
    changed = TaskEvidenceSnapshot.model_validate(body)
    async with db() as session, session.begin():
        await session.execute(
            update(AgentTaskEvidenceRow)
            .where(AgentTaskEvidenceRow.task_id == log.id)
            .values(snapshot=body, content_sha256=evidence_hash(changed))
        )
        if wrong == "scope":
            assignment = log.assignment.model_dump(mode="json")
            assignment["evidence_snapshot_id"] = body["evidence_snapshot_id"]
            await session.execute(
                update(AgentTaskRow).where(AgentTaskRow.id == log.id).values(assignment=assignment)
            )
    with pytest.raises(TaskFactConflict):
        await queries.get_evidence(started.run.id, log.id)
    clock.current += timedelta(seconds=1)
    restarted = LiveHttpFixture(started.run.observation_cutoff_at, clock)
    assert await run_live_worker(db, checkpoint_database_url, store, clock, restarted.source)
    assert (await store.get_run(started.run.id)).status != "succeeded"
    assert restarted.requests == []
