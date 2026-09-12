"""摘要实际输入的隔离、证据保留和 checkpoint 边界恢复。"""

import asyncio
from datetime import timedelta

import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.loop.contracts import LoopModelResult
from opsswarm.investigation.models import InvestigationDataSourceMode
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.checkpoint_fakes import CountingDataSource
from tests.integration.test_loop_graph import run_workers
from tests.live_task_helpers import LiveHttpFixture
from tests.loop_helpers import loop_policy
from tests.summary_context_helpers import CounterEvidenceSource, SummaryReplies
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("mode", ["frozen", "live"])
async def test_summary_input_preserves_evidence_and_isolates_incidents(
    database_session_factory, checkpoint_database_url, mode
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    starts = []
    for _ in range(2):
        parent, started = await started_run(
            db,
            workflow_version=WorkflowVersion.V2,
            data_source_mode=InvestigationDataSourceMode(mode),
            model_budget=loop_policy(),
            context_policy_version="summary-context-v1",
        )
        starts.append(started)
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    fixture = (
        LiveHttpFixture(starts[0].run.observation_cutoff_at, clock) if mode == "live" else None
    )
    reply = SummaryReplies(db)
    await run_workers(
        db,
        checkpoint_database_url,
        parent,
        clock,
        fixture.source if fixture else CounterEvidenceSource(),
        reply,
        count=2,
        mode=mode,
    )
    if fixture:
        fixture.assert_windows()
    for started in starts:
        run = await parent.get_run(started.run.id)
        assert run.status == "succeeded", run.last_error
        assert sorted(reply.seen[run.id]) == [
            "change",
            "change",
            "log",
            "log",
            "metric",
            "metric",
            "synthesis",
        ]
        data = reply.synthesis[run.id]
        assert data["incident"]["incident_id"] == str(started.run.incident_id)
        assert all(s["investigation_run_id"] == str(run.id) for s in data["working_summaries"])
        if mode == "frozen":
            log = next(
                s["summary"]
                for s in data["working_summaries"]
                if s["summary"]["report"]["specialist"] == "log"
            )
            assert len(log["evidence_catalog"]["entries"]) > len(log["read_evidence_ids"])
            assert any(
                e["attributes"].get("counter_evidence") == "仍有健康实例" for e in data["evidence"]
            )
        budget = await reply.calls.get_snapshot(run.id)
        assert budget.reserved_model_calls == 7 and budget.reserved_tool_calls == 6


async def test_summary_commit_before_checkpoint_recovers_without_repeating_professionals(
    database_session_factory, checkpoint_database_url, monkeypatch
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, started = await started_run(
        db,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="summary-context-v1",
    )
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    facts, reply = CollaborationQueries(db), SummaryReplies(db)
    original = InvestigationTaskStore.finish_attempt

    async def crash(self, run_id, task_id, attempt_id, **kwargs):
        outcome = await original(self, run_id, task_id, attempt_id, **kwargs)
        result = kwargs.get("result")
        if result is not None and result.report.specialist == "log":
            async with asyncio.timeout(15):
                while any(
                    t.status != "succeeded"
                    for t in (await facts.get_snapshot(run_id)).rounds[0].tasks
                ):
                    await asyncio.sleep(0.01)
            raise RuntimeError("模拟摘要提交后 checkpoint 前崩溃")
        return outcome

    with monkeypatch.context() as patch:
        patch.setattr(InvestigationTaskStore, "finish_attempt", crash)
        await run_workers(db, checkpoint_database_url, parent, clock, CountingDataSource(), reply)
    run = await parent.get_run(started.run.id)
    assert run.status == "queued", run.last_error
    tasks = (await facts.get_snapshot(run.id)).rounds[0].tasks
    summaries = {t.id: await facts.get_working_summary(run.id, t.id) for t in tasks}
    before = await reply.calls.get_snapshot(run.id)
    clock.current += timedelta(seconds=1)
    source = CountingDataSource()
    await run_workers(db, checkpoint_database_url, parent, clock, source, reply)
    run = await parent.get_run(run.id)
    assert run.status == "succeeded", run.last_error
    assert sum(source.calls.values()) == 0
    after = await reply.calls.get_snapshot(run.id)
    assert after.reserved_model_calls == before.reserved_model_calls + 1 == 7
    assert after.reserved_tool_calls == before.reserved_tool_calls == 6
    for task_id, summary in summaries.items():
        assert await facts.get_working_summary(run.id, task_id) == summary


async def test_synthesis_keeps_unknown_call_reference_after_professional_retry(
    database_session_factory, checkpoint_database_url, monkeypatch
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, started = await started_run(
        db,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="summary-context-v1",
    )
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    facts, reply = CollaborationQueries(db), SummaryReplies(db)
    original = CallBudgetStore.settle

    async def crash(self, run_id, reservation_id, **kwargs):
        result = kwargs.get("call_result")
        snapshot = await reply.calls.get_snapshot(run_id)
        call = next(c for c in snapshot.calls if c.id == reservation_id)
        if call.operation == "analyze_log" and isinstance(result, LoopModelResult):
            async with asyncio.timeout(15):
                while (
                    sum(
                        t.status == "succeeded"
                        for t in (await facts.get_snapshot(run_id)).rounds[0].tasks
                    )
                    != 2
                ):
                    await asyncio.sleep(0.01)
            raise RuntimeError("模拟日志模型返回后尚未提交便崩溃")
        return await original(self, run_id, reservation_id, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(CallBudgetStore, "settle", crash)
        await run_workers(db, checkpoint_database_url, parent, clock, CountingDataSource(), reply)
    run = await parent.get_run(started.run.id)
    assert run.status == "queued", run.last_error
    clock.current += timedelta(seconds=1)
    await run_workers(db, checkpoint_database_url, parent, clock, CountingDataSource(), reply)
    run = await parent.get_run(run.id)
    assert run.status == "succeeded", run.last_error
    records = reply.synthesis[run.id]["working_summaries"]
    log = next(s for s in records if s["summary"]["report"]["specialist"] == "log")
    unknown = [c for c in log["summary"]["calls"] if c["status"] == "unknown"]
    assert len(unknown) == 1 and unknown[0]["error_code"]
    assert unknown[0]["attempt_id"] != log["attempt_id"]
    budget = await reply.calls.get_snapshot(run.id)
    assert budget.reserved_model_calls == 8 and budget.reserved_tool_calls == 6
    assert budget.model_usage.unknown_model_calls == 1
