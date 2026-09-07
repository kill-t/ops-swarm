"""真实 PostgreSQL 中的不可变快照、事件与有效任务归属。"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update

from opsswarm.investigation.tasks.contracts import TaskOutcome
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence_tables import AgentTaskEvidenceRow
from opsswarm.investigation.tasks.queries import CollaborationQueries
from tests.task_evidence_helpers import (
    evidence_attempt,
    material,
    professional_result,
    save_evidence,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_concurrent_same_snapshot_is_idempotent_and_saved_before_report(
    database_session_factory,
):
    prepared = await evidence_attempt(database_session_factory)
    _, claim, _, _, task, _, _ = prepared
    first, second = await asyncio.gather(save_evidence(prepared), save_evidence(prepared))
    assert first == second
    queries = CollaborationQueries(database_session_factory)
    assert await queries.get_evidence(claim.run.id, task.id) == first
    assert await queries.get_result(claim.run.id, task.id) is None
    events = await queries.list_events(claim.run.id)
    assert sum(e.event_type == "task.evidence_saved" for e in events) == 1


async def test_conflicting_snapshots_cannot_overwrite_or_duplicate_event(database_session_factory):
    prepared = await evidence_attempt(database_session_factory)
    changed = (material()[0].model_copy(update={"summary": "另一份日志"}),)
    results = await asyncio.gather(
        save_evidence(prepared), save_evidence(prepared, changed), return_exceptions=True
    )
    assert sum(isinstance(r, TaskFactConflict) for r in results) == 1
    _, claim, _, _, task, _, _ = prepared
    queries = CollaborationQueries(database_session_factory)
    assert await queries.get_evidence(claim.run.id, task.id) in results
    assert (
        sum(e.event_type == "task.evidence_saved" for e in await queries.list_events(claim.run.id))
        == 1
    )


@pytest.mark.parametrize("status", ["reserved", "failed", "unknown"])
async def test_only_successful_tool_call_can_produce_snapshot(database_session_factory, status):
    prepared = await evidence_attempt(database_session_factory, call_status=status)
    with pytest.raises(TaskFactConflict) as caught:
        await save_evidence(prepared)
    assert caught.value.code == "task_evidence_call_required"
    _, claim, _, _, task, _, _ = prepared
    assert (
        await CollaborationQueries(database_session_factory).get_evidence(claim.run.id, task.id)
        is None
    )


async def test_scope_and_expired_lease_cannot_save_or_reuse(database_session_factory):
    first = await evidence_attempt(database_session_factory)
    second = await evidence_attempt(database_session_factory)
    _, claim, clock, _, task, attempt, store = first
    with pytest.raises(TaskFactConflict, match="有效执行尝试"):
        await store.save(
            claim.run.id,
            second[4].id,
            attempt.id,
            lease_token=claim.work_item.lease_token,
            evidence=material(),
        )
    original = await save_evidence(first)
    clock.current += timedelta(seconds=40)
    with pytest.raises(TaskFactConflict, match="租约"):
        await save_evidence(first)
    with pytest.raises(TaskFactConflict, match="租约"):
        await store.read_for_attempt(
            claim.run.id, task.id, attempt.id, lease_token=claim.work_item.lease_token
        )
    assert (
        await CollaborationQueries(database_session_factory).get_evidence(claim.run.id, task.id)
        == original
    )


async def test_result_must_match_saved_material_and_hash_corruption_is_rejected(
    database_session_factory,
):
    prepared = await evidence_attempt(database_session_factory)
    _, claim, _, tasks, task, attempt, _ = prepared
    original = await save_evidence(prepared)
    altered = (material()[0].model_copy(update={"summary": "篡改内容"}),)
    result = professional_result(task.assignment, altered)
    with pytest.raises(TaskFactConflict) as caught:
        await tasks.finish_attempt(
            claim.run.id,
            task.id,
            attempt.id,
            lease_token=claim.work_item.lease_token,
            outcome=TaskOutcome(status="succeeded", summary=result.report.summary),
            result=result,
        )
    assert caught.value.code == "task_evidence_result_mismatch"
    payload = original.snapshot.model_dump(mode="json")
    payload["evidence"][0]["summary"] = "数据库内容与散列失配"
    async with database_session_factory() as session, session.begin():
        await session.execute(
            update(AgentTaskEvidenceRow)
            .where(AgentTaskEvidenceRow.task_id == task.id)
            .values(snapshot=payload)
        )
    with pytest.raises(TaskFactConflict) as corrupt:
        await CollaborationQueries(database_session_factory).get_evidence(claim.run.id, task.id)
    assert corrupt.value.code == "task_evidence_corrupt"


async def test_snapshot_and_event_rollback_together(database_session_factory, monkeypatch):
    from opsswarm.investigation.tasks import evidence_store as store_module

    prepared = await evidence_attempt(database_session_factory)
    _, claim, _, _, task, _, _ = prepared
    queries = CollaborationQueries(database_session_factory)
    before = await queries.list_events(claim.run.id)
    original_append = store_module.append_task_event

    async def fail_after_flush(*args, **kwargs):
        await original_append(*args, **kwargs)
        await args[0].flush()
        raise RuntimeError("模拟快照与事件事务回滚")

    with monkeypatch.context() as patch:
        patch.setattr(store_module, "append_task_event", fail_after_flush)
        with pytest.raises(RuntimeError, match="事务回滚"):
            await save_evidence(prepared)
    assert await queries.get_evidence(claim.run.id, task.id) is None
    assert await queries.list_events(claim.run.id) == before
    assert await save_evidence(prepared)


async def test_result_without_snapshot_is_rejected(database_session_factory):
    prepared = await evidence_attempt(database_session_factory)
    _, claim, _, tasks, task, attempt, _ = prepared
    result = professional_result(task.assignment)
    with pytest.raises(TaskFactConflict) as caught:
        await tasks.finish_attempt(
            claim.run.id,
            task.id,
            attempt.id,
            lease_token=claim.work_item.lease_token,
            outcome=TaskOutcome(status="succeeded", summary=result.report.summary),
            result=result,
        )
    assert caught.value.code == "task_evidence_required"
    assert (
        await CollaborationQueries(database_session_factory).get_result(claim.run.id, task.id)
        is None
    )
