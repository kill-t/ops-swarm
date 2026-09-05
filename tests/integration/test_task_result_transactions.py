"""专业结果、状态与事件须同事务提交且不可被重复写覆盖。"""

import asyncio

import pytest

from opsswarm.investigation.tasks.contracts import TaskOutcome
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.results import SpecialistTaskResult
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from tests.task_facts_helpers import TaskClock, assignment, claimed_run
from tests.unit.investigation.test_task_results import material

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def prepare(db):
    _, claim = await claimed_run(db)
    tasks = InvestigationTaskStore(db, clock=TaskClock())
    queries = CollaborationQueries(db)
    planned = assignment()
    planned = planned.model_copy(
        update={
            "tasks": (planned.tasks[0].model_copy(update={"evidence_snapshot_id": "frozen:test"}),)
        }
    )
    await tasks.create_round(
        claim.run.id, lease_token=claim.work_item.lease_token, assignment=planned
    )
    task = (await queries.get_snapshot(claim.run.id)).rounds[0].tasks[0]
    start = await tasks.begin_attempt(
        claim.run.id, task.id, lease_token=claim.work_item.lease_token, expected_version=1
    )
    return claim, tasks, queries, task, start.attempt


async def test_result_and_events_rollback_then_idempotent_commit(
    database_session_factory, monkeypatch
):
    claim, tasks, queries, task, attempt = await prepare(database_session_factory)
    result = SpecialistTaskResult.model_validate(material())
    outcome = TaskOutcome(status="succeeded", summary=result.report.summary)
    before = await queries.list_events(claim.run.id)
    from opsswarm.investigation.tasks import store as store_module

    original_append = store_module.append_task_event

    async def fail_after_flush(*args, **kwargs):
        await original_append(*args, **kwargs)
        await args[0].flush()
        raise RuntimeError("模拟事件提交边界失败")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "append_task_event", fail_after_flush)
        with pytest.raises(RuntimeError, match="提交边界"):
            await tasks.finish_attempt(
                claim.run.id,
                task.id,
                attempt.id,
                lease_token=claim.work_item.lease_token,
                outcome=outcome,
                result=result,
            )
    assert await queries.get_result(claim.run.id, task.id) is None
    assert await queries.list_events(claim.run.id) == before
    assert (await queries.get_snapshot(claim.run.id)).rounds[0].tasks[0].status == "running"
    for _ in range(2):
        await tasks.finish_attempt(
            claim.run.id,
            task.id,
            attempt.id,
            lease_token=claim.work_item.lease_token,
            outcome=outcome,
            result=result,
        )
    stored = await queries.get_result(claim.run.id, task.id)
    assert stored.result == result and stored.attempt_id == attempt.id
    events = await queries.list_events(claim.run.id)
    assert [e.event_type for e in events] == [
        "round.created",
        "task.started",
        "task.succeeded",
        "round.completed",
    ]
    assert await queries.list_events(claim.run.id, after_cursor=2, limit=1) == (events[2],)
    assert await queries.list_events(claim.run.id, after_cursor=4) == ()
    altered = result.model_copy(
        update={"report": result.report.model_copy(update={"summary": "另一份报告"})}
    )
    with pytest.raises(TaskFactConflict, match="不同的终态"):
        await tasks.finish_attempt(
            claim.run.id,
            task.id,
            attempt.id,
            lease_token=claim.work_item.lease_token,
            outcome=TaskOutcome(status="succeeded", summary=altered.report.summary),
            result=altered,
        )


@pytest.mark.parametrize("invalid", ["snapshot", "role", "missing"])
async def test_assignment_mismatch_does_not_commit_result(database_session_factory, invalid):
    claim, tasks, queries, task, attempt = await prepare(database_session_factory)
    value = material()
    if invalid == "snapshot":
        value["evidence_snapshot_id"] = "frozen:another"
    elif invalid == "role":
        value["evidence"][0]["kind"] = "metric"
        value["report"]["specialist"] = "metric"
    result = None if invalid == "missing" else SpecialistTaskResult.model_validate(value)
    with pytest.raises(TaskFactConflict):
        await tasks.finish_attempt(
            claim.run.id,
            task.id,
            attempt.id,
            lease_token=claim.work_item.lease_token,
            outcome=TaskOutcome(status="succeeded", summary="专业发现"),
            result=result,
        )
    assert await queries.get_result(claim.run.id, task.id) is None
    assert len(await queries.list_events(claim.run.id)) == 2


async def test_concurrent_events_and_result_scope_do_not_cross_runs(database_session_factory):
    first, _, queries, task, _ = await prepare(database_session_factory)
    second, tasks, _, other, _ = await prepare(database_session_factory)
    with pytest.raises(TaskFactConflict, match="不属于"):
        await queries.get_result(second.run.id, task.id)
    assert all(
        e.investigation_run_id == second.run.id and e.task_id != task.id
        for e in await queries.list_events(second.run.id)
    )
    result = SpecialistTaskResult.model_validate(material())
    current = (await queries.get_snapshot(second.run.id)).rounds[0].tasks[0]
    await asyncio.gather(
        *(
            tasks.finish_attempt(
                second.run.id,
                other.id,
                current.attempts[0].id,
                lease_token=second.work_item.lease_token,
                outcome=TaskOutcome(status="succeeded", summary=result.report.summary),
                result=result,
            )
            for _ in range(2)
        )
    )
    assert len(await queries.list_events(first.run.id)) == 2
    assert len(await queries.list_events(second.run.id)) == 4
