"""快照提交两侧中断后的真实图恢复与调用预算。"""

import asyncio
from datetime import timedelta

import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.tasks.evidence_store import TaskEvidenceStore
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.checkpoint_fakes import CountingDataSource, SpecialistAnalystStub
from tests.integration.test_budgeted_graph import run_worker
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("committed", [False, True])
async def test_recovery_requeries_only_when_snapshot_was_not_committed(
    database_session_factory, checkpoint_database_url, monkeypatch, committed
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, started = await started_run(
        database_session_factory, workflow_version=WorkflowVersion.V2
    )
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    queries = CollaborationQueries(database_session_factory)
    original_save = TaskEvidenceStore.save

    async def interrupt_save(self, *args, **kwargs):
        if kwargs["evidence"][0].kind is not EvidenceKind.LOG:
            return await original_save(self, *args, **kwargs)
        async with asyncio.timeout(10):
            while True:
                snapshot = await queries.get_snapshot(started.run.id)
                if sum(t.status == "succeeded" for t in snapshot.rounds[0].tasks) == 2:
                    break
                await asyncio.sleep(0.01)
        if committed:
            await original_save(self, *args, **kwargs)
        raise RuntimeError("模拟取证快照提交边界中断")

    with monkeypatch.context() as patch:
        patch.setattr(TaskEvidenceStore, "save", interrupt_save)
        assert await run_worker(
            database_session_factory,
            checkpoint_database_url,
            parent,
            clock,
            CountingDataSource(),
            SpecialistAnalystStub(),
        )
    assert (await parent.get_run(started.run.id)).status == "queued"
    tasks = (await queries.get_snapshot(started.run.id)).rounds[0].tasks
    log = next(t for t in tasks if t.assignment.role is EvidenceKind.LOG)
    before = await queries.get_evidence(started.run.id, log.id)
    assert (before is not None) == committed
    assert await queries.get_result(started.run.id, log.id) is None
    budget_before = await CallBudgetQueries(database_session_factory).get_snapshot(started.run.id)
    assert budget_before.reserved_model_calls == 2
    assert budget_before.reserved_tool_calls == 3

    clock.current += timedelta(seconds=1)
    recovered = CountingDataSource()
    assert await run_worker(
        database_session_factory,
        checkpoint_database_url,
        parent,
        clock,
        recovered,
        SpecialistAnalystStub(),
    )
    assert (await parent.get_run(started.run.id)).status == "succeeded"
    assert sum(recovered.calls.values()) == (0 if committed else 1)
    after = await queries.get_evidence(started.run.id, log.id)
    if committed:
        assert after == before
    else:
        assert after is not None and after.attempt_id != log.attempts[0].id
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(started.run.id)
    assert budget.reserved_model_calls == 4
    assert budget.reserved_tool_calls == (3 if committed else 4)
    events = await queries.list_events(started.run.id)
    assert sum(e.event_type == "task.evidence_saved" for e in events) == 3
