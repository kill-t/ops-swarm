"""摘要与任务结算原子保存、来源验证及恢复边界。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError

from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.artifact_tables import AgentTaskEventRow, AgentTaskResultRow
from opsswarm.investigation.tasks.contracts import TaskOutcome
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.tables import AgentTaskRow
from opsswarm.investigation.working_summary.tables import AgentTaskWorkingSummaryRow
from tests.loop_helpers import prepared_loop, reserve_loop
from tests.working_summary_helpers import analyzed_task, finish

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_summary_atomic_idempotent_and_run_scoped(database_session_factory):
    db = database_session_factory
    prepared, result = await analyzed_task(db)
    _, claim, _, _, task, attempt, _ = prepared
    queries = CollaborationQueries(db)
    assert claim.run.working_summary_policy_version == "task-working-summary-v1"
    assert await queries.get_working_summary(claim.run.id, task.id) is None
    first, second = await asyncio.gather(finish(prepared, result), finish(prepared, result))
    assert first == second
    saved = await queries.get_working_summary(claim.run.id, task.id)
    assert saved.summary.report == result.report
    assert saved.summary.read_evidence_ids == result.report.evidence_ids
    assert len(saved.summary.calls) == 4
    assert set(saved.summary.read_evidence_ids) <= {
        e.evidence_id for e in saved.summary.evidence_catalog.entries
    }
    assert "attributes" not in saved.model_dump_json()
    assert await queries.get_working_summary(claim.run.id, task.id) == saved
    assert (await queries.get_result(claim.run.id, task.id)).attempt_id == attempt.id
    with pytest.raises(TaskFactConflict):
        await queries.get_working_summary(claim.run.id, uuid4())
    for statement in (
        "UPDATE agent_task_working_summaries SET content_sha256 = content_sha256",
        "DELETE FROM agent_task_working_summaries",
    ):
        with pytest.raises(DBAPIError):
            async with db() as session, session.begin():
                await session.execute(text(statement))


@pytest.mark.parametrize(
    "damage", ["report", "reserved", "missing_final", "wrong_source", "tool_arguments"]
)
async def test_invalid_summary_rolls_back_success_and_event(database_session_factory, damage):
    db = database_session_factory
    prepared, result = await analyzed_task(db)
    _, claim, _, _, task, attempt, _ = prepared
    if damage == "report":
        result = result.model_copy(
            update={"report": result.report.model_copy(update={"summary": "改写"})}
        )
    else:
        async with db() as session, session.begin():
            final = await session.scalar(
                select(CallReservationRow)
                .where(
                    CallReservationRow.task_attempt_id == attempt.id,
                    CallReservationRow.kind == "model",
                )
                .order_by(CallReservationRow.invocation_index.desc())
            )
            if damage == "tool_arguments":
                tool = await session.scalar(
                    select(CallReservationRow).where(
                        CallReservationRow.task_attempt_id == attempt.id,
                        CallReservationRow.tool_request.is_not(None),
                    )
                )
                tool.tool_request = {
                    **tool.tool_request,
                    "arguments": {
                        **tool.tool_request["arguments"],
                        "evidence_id": "other-evidence",
                    },
                }
            elif damage == "missing_final":
                final.call_result = None
            elif damage == "wrong_source":
                output = dict(final.call_result)
                output["messages"] = [dict(m) for m in output["messages"]]
                output["messages"][0]["tool_calls"] = [dict(output["messages"][0]["tool_calls"][0])]
                output["messages"][0]["tool_calls"][0]["args"] = {
                    **output["messages"][0]["tool_calls"][0]["args"],
                    "summary": "篡改来源",
                }
                final.call_result = output
            else:
                final.status = "reserved"
                final.finished_at = None
                final.model_usage = None
                final.call_result = None
    with pytest.raises(TaskFactConflict):
        await finish(prepared, result)
    async with db() as session:
        assert (await session.get(AgentTaskRow, task.id)).status == "running"
        assert await session.get(AgentTaskResultRow, task.id) is None
        assert await session.get(AgentTaskWorkingSummaryRow, task.id) is None
        assert not list(
            await session.scalars(
                select(AgentTaskEventRow).where(
                    AgentTaskEventRow.task_id == task.id,
                    AgentTaskEventRow.event_type == "task.succeeded",
                )
            )
        )


async def test_retry_summary_keeps_prior_failed_call_without_reset(database_session_factory):
    db = database_session_factory
    prepared, packet, budget, service = await prepared_loop(db)
    _, claim, _, tasks, task, attempt, _ = prepared
    failed = await reserve_loop(prepared, packet, budget)
    await budget.settle(
        claim.run.id,
        failed.record.id,
        lease_token=claim.work_item.lease_token,
        status="failed",
        error_code="operation_failed",
    )
    await tasks.finish_attempt(
        claim.run.id,
        task.id,
        attempt.id,
        lease_token=claim.work_item.lease_token,
        outcome=TaskOutcome(
            status="failed", error_code="specialist_operation_failed", retryable=True
        ),
    )
    snapshot = await CollaborationQueries(db).get_snapshot(claim.run.id)
    task = next(t for t in snapshot.rounds[0].tasks if t.id == task.id)
    new = await tasks.begin_attempt(
        claim.run.id,
        task.id,
        lease_token=claim.work_item.lease_token,
        expected_version=task.version,
    )
    prepared = (*prepared[:4], task, new.attempt, prepared[6])
    prepared, result = await analyzed_task(db, prepared=prepared, store=budget, service=service)
    await finish(prepared, result)
    saved = await CollaborationQueries(db).get_working_summary(claim.run.id, task.id)
    prior = next(c for c in saved.summary.calls if c.call_id == failed.record.id)
    assert prior.status == "failed" and prior.attempt_id == attempt.id
    assert saved.attempt_id == new.attempt.id
    assert len(saved.summary.calls) == 5
    assert [c.sequence for c in saved.summary.calls] == sorted(
        c.sequence for c in saved.summary.calls
    )


async def test_legacy_loop_policy_does_not_backfill_summary(database_session_factory):
    db = database_session_factory
    prepared, result = await analyzed_task(db)
    _, claim, _, _, task, _, _ = prepared
    async with db() as session, session.begin():
        await session.execute(
            update(InvestigationRunRow)
            .where(InvestigationRunRow.id == claim.run.id)
            .values(working_summary_policy_version=None)
        )
    await finish(prepared, result)
    assert await CollaborationQueries(db).get_working_summary(claim.run.id, task.id) is None
    assert await CollaborationQueries(db).get_result(claim.run.id, task.id) is not None


async def test_expired_lease_cannot_commit_summary(database_session_factory):
    prepared, result = await analyzed_task(database_session_factory)
    prepared[2].current += timedelta(minutes=10)
    with pytest.raises(Exception, match="租约|lease"):
        await finish(prepared, result)
    async with database_session_factory() as session:
        assert await session.get(AgentTaskWorkingSummaryRow, prepared[4].id) is None


async def test_query_rechecks_source_integrity(database_session_factory):
    db = database_session_factory
    prepared, result = await analyzed_task(db)
    await finish(prepared, result)
    _, claim, _, _, task, _, _ = prepared
    async with db() as session, session.begin():
        row = await session.get(AgentTaskResultRow, task.id)
        changed = dict(row.result)
        changed["report"] = {**changed["report"], "confidence": 0.1}
        row.result = changed
    with pytest.raises(TaskFactConflict):
        await CollaborationQueries(db).get_working_summary(claim.run.id, task.id)
    with pytest.raises(TaskFactConflict):
        await CollaborationQueries(db).get_result(claim.run.id, task.id)
