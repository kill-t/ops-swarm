"""真实 PostgreSQL 中的专业任务幂等、竞争、租约和隔离测试。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

from opsswarm.investigation.models import WorkItemError
from opsswarm.investigation.tasks.contracts import RoundAssignment, TaskOutcome
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from opsswarm.investigation.tasks.tables import (
    AgentTaskAttemptRow,
    AgentTaskRow,
    InvestigationRoundRow,
)
from tests.task_facts_helpers import BASE_TIME, TaskClock, assignment, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
SUCCESS = TaskOutcome(status="succeeded", summary="专业处理已完成，尚未形成事故结论")


async def test_round_creation_is_atomic_and_idempotent_under_race(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    clock = TaskClock()
    store = InvestigationTaskStore(database_session_factory, clock=clock)
    params = dict(lease_token=claim.work_item.lease_token, assignment=assignment(count=3))
    results = await asyncio.gather(*(store.create_round(claim.run.id, **params) for _ in range(4)))
    assert sum(created for _, created in results) == 1
    assert len({round_id for round_id, _ in results}) == 1
    snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    assert [task.assignment.role for task in snapshot.rounds[0].tasks] == [
        "change",
        "log",
        "metric",
    ]
    assert len(snapshot.rounds) == 1
    assert all(task.attempt_count == 0 and task.version == 1 for task in snapshot.rounds[0].tasks)
    reordered = assignment(count=3).model_copy(
        update={"tasks": tuple(reversed(assignment(count=3).tasks))}
    )
    assert (
        await store.create_round(
            claim.run.id, lease_token=claim.work_item.lease_token, assignment=reordered
        )
    )[1] is False


async def test_conflicting_rounds_leave_original_unchanged(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    store = InvestigationTaskStore(database_session_factory, clock=TaskClock())
    await store.create_round(
        claim.run.id, lease_token=claim.work_item.lease_token, assignment=assignment()
    )
    for spec, code in (
        (assignment().model_copy(update={"goal": "不同分派"}), "round_content_conflict"),
        (assignment(round_number=2), "round_sequence_conflict"),
        (assignment(round_number=3), "round_sequence_conflict"),
    ):
        with pytest.raises(TaskFactConflict) as raised:
            await store.create_round(
                claim.run.id, lease_token=claim.work_item.lease_token, assignment=spec
            )
        assert raised.value.code == code
    snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    assert len(snapshot.rounds) == 1
    assert snapshot.rounds[0].goal == assignment().goal


async def test_service_scope_and_wrong_lease_are_rejected(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    store = InvestigationTaskStore(database_session_factory, clock=TaskClock())
    spec = RoundAssignment(
        round_number=1,
        goal="越界",
        tasks=(assignment().tasks[0].model_copy(update={"service_name": "another-service"}),),
    )
    for token, value, code in (
        (claim.work_item.lease_token, spec, "task_scope_conflict"),
        (uuid4(), assignment(), "task_lease_lost"),
    ):
        with pytest.raises(TaskFactConflict) as raised:
            await store.create_round(claim.run.id, lease_token=token, assignment=value)
        assert raised.value.code == code
    assert (
        await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    ).rounds == ()


async def prepare(factory, *, max_attempts=3):
    parent_store, claim = await claimed_run(factory)
    clock = TaskClock()
    store = InvestigationTaskStore(factory, clock=clock)
    queries = CollaborationQueries(factory)
    await store.create_round(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        assignment=assignment(max_attempts=max_attempts),
    )
    task = (await queries.get_snapshot(claim.run.id)).rounds[0].tasks[0]
    return parent_store, claim, clock, store, queries, task


async def test_concurrent_begin_and_conflicting_versions(database_session_factory):
    _, claim, _, store, queries, task = await prepare(database_session_factory)
    kwargs = dict(lease_token=claim.work_item.lease_token, expected_version=1)
    starts = await asyncio.gather(
        *(store.begin_attempt(claim.run.id, task.id, **kwargs) for _ in range(3))
    )
    assert sum(start.created for start in starts) == 1
    assert len({start.attempt.id for start in starts}) == 1
    for version, code in ((-1, "task_version_conflict"), (2, "task_state_conflict")):
        with pytest.raises(TaskFactConflict) as raised:
            await store.begin_attempt(
                claim.run.id,
                task.id,
                lease_token=claim.work_item.lease_token,
                expected_version=version,
            )
        assert raised.value.code == code
    record = (await queries.get_snapshot(claim.run.id)).rounds[0].tasks[0]
    assert (record.version, record.attempt_count, record.status) == (2, 1, "running")


async def test_success_replay_is_immutable_and_next_round_has_new_task(database_session_factory):
    _, claim, _, store, queries, task = await prepare(database_session_factory)
    start = await store.begin_attempt(
        claim.run.id, task.id, lease_token=claim.work_item.lease_token, expected_version=1
    )
    args = (claim.run.id, task.id, start.attempt.id)
    kwargs = dict(lease_token=claim.work_item.lease_token, outcome=SUCCESS)
    first = await store.finish_attempt(*args, **kwargs)
    assert await store.finish_attempt(*args, **kwargs) == first
    with pytest.raises(TaskFactConflict, match="不同的终态"):
        await store.finish_attempt(
            *args,
            lease_token=claim.work_item.lease_token,
            outcome=TaskOutcome(status="failed", error_code="timeout"),
        )
    finished = (await queries.get_snapshot(claim.run.id)).rounds[0]
    assert finished.status == "completed"
    assert finished.tasks[0].version == 3
    assert finished.completed_at == first.finished_at
    await store.create_round(
        claim.run.id, lease_token=claim.work_item.lease_token, assignment=assignment(round_number=2)
    )
    rounds = (await queries.get_snapshot(claim.run.id)).rounds
    assert [item.round_number for item in rounds] == [1, 2]
    assert rounds[0].tasks[0].id != rounds[1].tasks[0].id


async def test_failure_retry_preserves_task_and_stops_at_limit(database_session_factory):
    _, claim, _, store, queries, original = await prepare(database_session_factory, max_attempts=2)
    attempts = []
    for number in (1, 2):
        task = (await queries.get_snapshot(claim.run.id)).rounds[0].tasks[0]
        start = await store.begin_attempt(
            claim.run.id,
            task.id,
            lease_token=claim.work_item.lease_token,
            expected_version=task.version,
        )
        attempts.append(start.attempt.id)
        await store.finish_attempt(
            claim.run.id,
            task.id,
            start.attempt.id,
            lease_token=claim.work_item.lease_token,
            outcome=TaskOutcome(status="failed", error_code="source_timeout", retryable=True),
        )
        task = (await queries.get_snapshot(claim.run.id)).rounds[0].tasks[0]
        assert task.id == original.id
        assert task.attempt_count == number
        assert task.status == ("pending" if number == 1 else "failed")
    assert len(set(attempts)) == 2
    assert (await queries.get_snapshot(claim.run.id)).rounds[0].status == "failed"
    with pytest.raises(TaskFactConflict, match="已结束任务"):
        await store.begin_attempt(
            claim.run.id,
            task.id,
            lease_token=claim.work_item.lease_token,
            expected_version=task.version,
        )


@pytest.mark.parametrize("max_attempts", [1, 3])
async def test_lease_takeover_rejects_late_results_and_preserves_attempts(
    database_session_factory, max_attempts
):
    parent_store, claim, clock, store, queries, task = await prepare(
        database_session_factory, max_attempts=max_attempts
    )
    old = await store.begin_attempt(
        claim.run.id, task.id, lease_token=claim.work_item.lease_token, expected_version=1
    )
    clock.current = BASE_TIME + timedelta(seconds=33)
    with pytest.raises(TaskFactConflict, match="租约无效"):
        await store.finish_attempt(
            claim.run.id,
            task.id,
            old.attempt.id,
            lease_token=claim.work_item.lease_token,
            outcome=SUCCESS,
        )
    replacement = await parent_store.claim_next(
        worker_id="replacement", occurred_at=clock.current, lease_duration=timedelta(seconds=30)
    )
    assert replacement is not None
    task = (await queries.get_snapshot(claim.run.id)).rounds[0].tasks[0]
    assert task.version == 3
    if max_attempts == 1:
        with pytest.raises(TaskFactConflict, match="已结束任务"):
            await store.begin_attempt(
                claim.run.id,
                task.id,
                lease_token=replacement.work_item.lease_token,
                expected_version=task.version,
            )
    else:
        resumed = await store.begin_attempt(
            claim.run.id,
            task.id,
            lease_token=replacement.work_item.lease_token,
            expected_version=task.version,
        )
    with pytest.raises(TaskFactConflict, match="旧 Worker"):
        await store.finish_attempt(
            claim.run.id,
            task.id,
            old.attempt.id,
            lease_token=replacement.work_item.lease_token,
            outcome=SUCCESS,
        )
    task = (await queries.get_snapshot(claim.run.id)).rounds[0].tasks[0]
    assert task.attempts[0].status == "lease_expired"
    if max_attempts == 1:
        assert task.status == "failed" and task.attempt_count == 1
    else:
        assert resumed.created and resumed.attempt.id != old.attempt.id
        assert task.attempt_count == 2
        await store.finish_attempt(
            claim.run.id,
            task.id,
            resumed.attempt.id,
            lease_token=replacement.work_item.lease_token,
            outcome=SUCCESS,
        )
        assert (await queries.get_snapshot(claim.run.id)).rounds[0].status == "completed"


async def test_parent_retry_pending_blocks_child_writes(database_session_factory):
    parent_store, claim, clock, store, queries, task = await prepare(database_session_factory)
    await parent_store.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock.current,
        error=WorkItemError(error_code="test_failure", message="测试父任务失败"),
        retry_delay=timedelta(seconds=10),
    )
    with pytest.raises(TaskFactConflict, match="租约无效"):
        await store.begin_attempt(
            claim.run.id, task.id, lease_token=claim.work_item.lease_token, expected_version=1
        )
    assert (await queries.get_snapshot(claim.run.id)).rounds[0].tasks[0].attempt_count == 0


async def test_expiry_at_commit_rolls_back_whole_round(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    times = iter([BASE_TIME + timedelta(seconds=3), BASE_TIME + timedelta(seconds=33)])
    store = InvestigationTaskStore(database_session_factory, clock=lambda: next(times))
    with pytest.raises(TaskFactConflict, match="租约无效"):
        await store.create_round(
            claim.run.id, lease_token=claim.work_item.lease_token, assignment=assignment()
        )
    async with database_session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(InvestigationRoundRow)) == 0
        assert await session.scalar(select(func.count()).select_from(AgentTaskRow)) == 0


async def test_cross_run_writes_are_rejected_by_service_and_database(database_session_factory):
    _, first, _, store, queries, task = await prepare(database_session_factory)
    _, second = await claimed_run(database_session_factory)
    with pytest.raises(TaskFactConflict, match="不属于该调查"):
        await store.begin_attempt(
            second.run.id, task.id, lease_token=second.work_item.lease_token, expected_version=1
        )
    start = await store.begin_attempt(
        first.run.id, task.id, lease_token=first.work_item.lease_token, expected_version=1
    )
    async with database_session_factory() as session:
        with pytest.raises(IntegrityError):
            async with session.begin():
                await session.execute(
                    update(AgentTaskAttemptRow)
                    .where(AgentTaskAttemptRow.id == start.attempt.id)
                    .values(investigation_attempt_id=second.attempt.id)
                )
    assert (await queries.get_snapshot(second.run.id)).rounds == ()


async def test_round_does_not_finish_until_parallel_tasks_settle(database_session_factory):
    _, claim = await claimed_run(database_session_factory)
    store = InvestigationTaskStore(database_session_factory, clock=TaskClock())
    queries = CollaborationQueries(database_session_factory)
    await store.create_round(
        claim.run.id, lease_token=claim.work_item.lease_token, assignment=assignment(count=2)
    )
    tasks = (await queries.get_snapshot(claim.run.id)).rounds[0].tasks
    starts = await asyncio.gather(
        *(
            store.begin_attempt(
                claim.run.id, task.id, lease_token=claim.work_item.lease_token, expected_version=1
            )
            for task in tasks
        )
    )
    await store.finish_attempt(
        claim.run.id,
        tasks[0].id,
        starts[0].attempt.id,
        lease_token=claim.work_item.lease_token,
        outcome=TaskOutcome(status="failed", error_code="unavailable"),
    )
    assert (await queries.get_snapshot(claim.run.id)).rounds[0].status == "running"
    await store.finish_attempt(
        claim.run.id,
        tasks[1].id,
        starts[1].attempt.id,
        lease_token=claim.work_item.lease_token,
        outcome=SUCCESS,
    )
    assert (await queries.get_snapshot(claim.run.id)).rounds[0].status == "failed"
