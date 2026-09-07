"""真实 PostgreSQL 的预留竞争、重试、隔离和结算。"""

import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import select, update

from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallGrant, CallOperation
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.models import WorkItemError
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.task_facts_helpers import BASE_TIME, TaskClock, assignment, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def prepare(db, policy=None, model_budget=None):
    parent, claim = await claimed_run(
        db, workflow_version=WorkflowVersion.V2, call_budget=policy, model_budget=model_budget
    )
    if model_budget is not None:
        # 此处覆盖第 69 步原始预算契约；新任务上下文另有材料完整性测试。
        async with db() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(
                    InvestigationRunRow.id == claim.run.id,
                )
                .values(context_policy_version=None)
            )
    clock = TaskClock()
    tasks = InvestigationTaskStore(db, clock=clock)
    await tasks.create_round(
        claim.run.id, lease_token=claim.work_item.lease_token, assignment=assignment(count=3)
    )
    snapshot = await CollaborationQueries(db).get_snapshot(claim.run.id)
    attempts = {}
    for task in snapshot.rounds[0].tasks:
        started = await tasks.begin_attempt(
            claim.run.id,
            task.id,
            lease_token=claim.work_item.lease_token,
            expected_version=task.version,
        )
        attempts[task.assignment.role.value] = started.attempt
    return parent, claim, clock, tasks, attempts, CallBudgetStore(db, clock=clock)


async def reserve(store, claim, attempt, operation):
    return await store.reserve(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        operation=operation,
        task_id=attempt.task_id,
        task_attempt_id=attempt.id,
    )


async def test_parallel_roles_compete_for_one_model_slot(database_session_factory):
    _, claim, _, _, attempts, store = await prepare(
        database_session_factory, CallBudgetPolicy(max_model_calls=1)
    )
    results = await asyncio.gather(
        *(
            reserve(store, claim, attempts[role], CallOperation(f"analyze_{role}"))
            for role in attempts
        ),
        return_exceptions=True,
    )
    grants = [item for item in results if isinstance(item, CallGrant)]
    assert len(grants) == 1 and sum(isinstance(item, CallBudgetExceeded) for item in results) == 2
    original = grants[0].record
    repeated = await store.reserve(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        operation=original.operation,
        task_id=original.task_id,
        task_attempt_id=original.task_attempt_id,
    )
    assert not repeated.created and repeated.record.id == original.id
    snapshot = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert snapshot.reserved_model_calls == 1 and len(snapshot.calls) == 1
    for _ in range(2):
        await store.settle(
            claim.run.id, original.id, lease_token=claim.work_item.lease_token, status="succeeded"
        )
    with pytest.raises(TaskFactConflict, match="另一种结算"):
        await store.settle(
            claim.run.id,
            original.id,
            lease_token=claim.work_item.lease_token,
            status="failed",
            error_code="operation_failed",
        )


async def test_parent_retry_keeps_unknown_charge_and_rejects_late_settlement(
    database_session_factory,
):
    parent, claim, clock, tasks, attempts, store = await prepare(
        database_session_factory, CallBudgetPolicy(max_model_calls=1)
    )
    original = await reserve(store, claim, attempts["log"], CallOperation.ANALYZE_LOG)
    await parent.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="模拟预留后中断"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await parent.claim_next(
        worker_id="replacement", occurred_at=clock(), lease_duration=timedelta(seconds=30)
    )
    snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    log = next(t for t in snapshot.rounds[0].tasks if t.assignment.role == "log")
    attempt = await tasks.begin_attempt(
        claim.run.id,
        log.id,
        lease_token=replacement.work_item.lease_token,
        expected_version=log.version,
    )
    with pytest.raises(CallBudgetExceeded):
        await reserve(store, replacement, attempt.attempt, CallOperation.ANALYZE_LOG)
    for token in (claim.work_item.lease_token, replacement.work_item.lease_token):
        with pytest.raises(TaskFactConflict):
            await store.settle(
                claim.run.id, original.record.id, lease_token=token, status="succeeded"
            )
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert budget.reserved_model_calls == 1 and budget.calls[0].status == "unknown"
    assert replacement.run.started_at == claim.run.started_at


async def test_scope_and_deadline_fail_before_reservation(database_session_factory):
    _, first, clock, _, attempts, store = await prepare(
        database_session_factory, CallBudgetPolicy(max_duration_seconds=3)
    )
    _, second, _, _, other, _ = await prepare(database_session_factory)
    for claim, attempt, operation in (
        (first, attempts["log"], CallOperation.QUERY_METRICS),
        (first, other["log"], CallOperation.QUERY_LOGS),
        (second, attempts["log"], CallOperation.QUERY_LOGS),
    ):
        with pytest.raises(TaskFactConflict, match="不属于"):
            await reserve(store, claim, attempt, operation)
    clock.current = BASE_TIME + timedelta(seconds=5)
    with pytest.raises(CallBudgetExceeded):
        await reserve(store, first, attempts["log"], CallOperation.QUERY_LOGS)
    assert (
        await CallBudgetQueries(database_session_factory).get_snapshot(first.run.id)
    ).calls == ()


async def test_expired_parent_marks_open_calls_unknown(database_session_factory):
    parent, claim, clock, _, attempts, store = await prepare(database_session_factory)
    await reserve(store, claim, attempts["log"], CallOperation.QUERY_LOGS)
    clock.current = BASE_TIME + timedelta(seconds=33)
    await parent.claim_next(
        worker_id="takeover", occurred_at=clock(), lease_duration=timedelta(seconds=30)
    )
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert budget.reserved_tool_calls == 1 and budget.calls[0].status == "unknown"


async def test_failed_tool_attempt_still_uses_task_quota(database_session_factory):
    from opsswarm.investigation.tasks.contracts import TaskOutcome

    _, claim, _, tasks, attempts, store = await prepare(
        database_session_factory, CallBudgetPolicy(max_tool_calls_per_task=1)
    )
    log = attempts["log"]
    grant = await reserve(store, claim, log, CallOperation.QUERY_LOGS)
    await store.settle(
        claim.run.id,
        grant.record.id,
        lease_token=claim.work_item.lease_token,
        status="failed",
        error_code="operation_failed",
    )
    await tasks.finish_attempt(
        claim.run.id,
        log.task_id,
        log.id,
        lease_token=claim.work_item.lease_token,
        outcome=TaskOutcome(status="failed", error_code="tool_failed", retryable=True),
    )
    task = next(
        t
        for t in (await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id))
        .rounds[0]
        .tasks
        if t.id == log.task_id
    )
    retry = await tasks.begin_attempt(
        claim.run.id,
        task.id,
        lease_token=claim.work_item.lease_token,
        expected_version=task.version,
    )
    with pytest.raises(CallBudgetExceeded) as caught:
        await reserve(store, claim, retry.attempt, CallOperation.QUERY_LOGS)
    assert caught.value.code == "task_tool_budget_exceeded"


async def test_legacy_v2_keeps_unknown_budget_and_migration_does_not_invent_history(
    database_session_factory,
):
    from opsswarm.investigation.persistence.tables import InvestigationRunRow

    _, claim = await claimed_run(database_session_factory, workflow_version=WorkflowVersion.V2)
    async with database_session_factory() as session, session.begin():
        run = await session.scalar(
            select(InvestigationRunRow).where(InvestigationRunRow.id == claim.run.id)
        )
        run.call_budget = None
        # 模拟引入预算之前的真实历史行，同时保留后续策略的空值语义。
        run.evidence_policy_version = None
        run.context_policy_version = None
    snapshot = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert (
        snapshot.policy is None
        and snapshot.reserved_model_calls is None
        and snapshot.reserved_tool_calls is None
    )
    with pytest.raises(TaskFactConflict, match="没有固定"):
        await CallBudgetStore(database_session_factory, clock=TaskClock()).check(
            claim.run.id, lease_token=claim.work_item.lease_token
        )
