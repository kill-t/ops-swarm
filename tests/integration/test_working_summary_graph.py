"""摘要已提交但 checkpoint 尚未推进时，恢复复用同一份工作材料。"""

import asyncio
from datetime import timedelta

import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.checkpoint_fakes import CountingDataSource
from tests.integration.test_loop_graph import LoopReplies, run_workers
from tests.loop_helpers import loop_policy
from tests.task_facts_helpers import BASE_TIME, TaskClock, started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_recovery_reuses_committed_summaries_without_new_professional_calls(
    database_session_factory, checkpoint_database_url, monkeypatch
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    parent, started = await started_run(
        db,
        workflow_version=WorkflowVersion.V2,
        model_budget=loop_policy(),
        context_policy_version="evidence-read-v1",
    )
    clock = TaskClock(BASE_TIME + timedelta(seconds=2))
    facts = CollaborationQueries(db)
    reply = LoopReplies(db)
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
    before = {t.id: await facts.get_working_summary(run.id, t.id) for t in tasks}
    assert all(before.values())
    before_calls = await reply.calls.get_snapshot(run.id)
    clock.current += timedelta(seconds=1)
    source = CountingDataSource()
    await run_workers(db, checkpoint_database_url, parent, clock, source, reply)
    run = await parent.get_run(run.id)
    assert run.status == "succeeded", run.last_error
    assert sum(source.calls.values()) == 0
    after_calls = await reply.calls.get_snapshot(run.id)
    assert after_calls.reserved_tool_calls == before_calls.reserved_tool_calls == 6
    assert after_calls.reserved_model_calls == 7
    for task_id, summary in before.items():
        assert await facts.get_working_summary(run.id, task_id) == summary
