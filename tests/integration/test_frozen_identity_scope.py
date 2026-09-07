"""旧身份授权不能绕过完整任务分派与内容边界。"""

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from sqlalchemy import update

from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.runtime import FixedTaskRuntime
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from opsswarm.investigation.tasks.tables import AgentTaskRow
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.checkpoint_fakes import CountingDataSource
from tests.integration.test_fixed_task_graph import context, factory
from tests.task_facts_helpers import TaskClock, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("invalid", ["unknown", "mixed", "content", "window"])
async def test_invalid_old_scope_fails_before_queries(database_session_factory, invalid):
    db = database_session_factory
    _, claim = await claimed_run(db, workflow_version=WorkflowVersion.V2)
    source = CountingDataSource()
    aliases = sorted(source.legacy_snapshot_ids)
    saved_id = "frozen:" + "0" * 64 if invalid == "unknown" else aliases[0]
    queries = CollaborationQueries(db)
    runtime = FixedTaskRuntime(
        run_id=claim.run.id,
        lease_token=claim.work_item.lease_token,
        store=InvestigationTaskStore(db, clock=TaskClock()),
        queries=queries,
        snapshot_id=saved_id,
        snapshot_time=source.snapshot_time,
    )
    await runtime.prepare(claim.request)
    before = await queries.get_snapshot(claim.run.id)
    if invalid in ("mixed", "window"):
        task = before.rounds[0].tasks[0]
        assignment = task.assignment.model_dump(mode="json")
        if invalid == "mixed":
            assignment["evidence_snapshot_id"] = aliases[1]
        else:
            # 旧身份完全一致也不能掩盖任务表与分派内容不一致。
            assignment["window_start"] = "2026-01-01T02:01:00Z"
        async with db() as session, session.begin():
            await session.execute(
                update(AgentTaskRow).where(AgentTaskRow.id == task.id).values(assignment=assignment)
            )
    if invalid == "content":
        source = FrozenScenarioDataSource(
            RELEASE_500_SCENARIO.model_copy(update={"scenario_id": "changed"})
        )
    graph = factory(db, InMemorySaver(), source, TaskClock())(context(claim))
    with pytest.raises(TaskFactConflict):
        await graph.investigate(claim.request, thread_id=claim.run.graph_thread_id)
    if isinstance(source, CountingDataSource):
        assert sum(source.calls.values()) == 0
    after = await queries.get_snapshot(claim.run.id)
    assert all(t.attempt_count == 0 for t in after.rounds[0].tasks)
