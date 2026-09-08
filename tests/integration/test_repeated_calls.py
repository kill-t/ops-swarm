"""同一尝试重复操作仍受持久预算、幂等与恢复约束。"""

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallGrant, CallOperation
from opsswarm.investigation.budget.model_policy import allocate_input
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.models import WorkItemError
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from tests.integration.test_call_budget_store import prepare
from tests.model_budget_helpers import model_input, model_policy

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def reserve_slot(store, claim, attempt, index, **kwargs):
    return await store.reserve(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        task_id=attempt.task_id,
        task_attempt_id=attempt.id,
        invocation_index=index,
        **{"operation": CallOperation.QUERY_LOGS, **kwargs},
    )


async def test_same_slot_race_is_idempotent_and_distinct_slots_share_limit(
    database_session_factory,
):
    _, claim, _, _, attempts, store = await prepare(
        database_session_factory, CallBudgetPolicy(max_tool_calls_per_task=2)
    )
    log = attempts["log"]
    first = await asyncio.gather(*(reserve_slot(store, claim, log, 7) for _ in range(3)))
    assert sum(grant.created for grant in first) == 1
    assert len({grant.record.id for grant in first}) == 1
    results = await asyncio.gather(
        *(reserve_slot(store, claim, log, i) for i in (8, 9, 10)), return_exceptions=True
    )
    assert sum(isinstance(r, CallGrant) for r in results) == 1
    errors = [r for r in results if isinstance(r, CallBudgetExceeded)]
    assert len(errors) == 2 and all(e.code == "task_tool_budget_exceeded" for e in errors)
    assert not (await reserve_slot(store, claim, log, 7)).created
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert budget.reserved_tool_calls == 2
    assert [call.sequence for call in budget.calls] == [1, 2]
    assert budget.calls[0].invocation_index == 7


@pytest.mark.parametrize("limit", ["input", "output", "cost", "count"])
async def test_same_model_operation_shares_all_run_limits(database_session_factory, limit):
    allocation = allocate_input(model_policy(), model_input(), prompt_version="test-v1")
    limits = {
        "input": {"max_input_tokens": allocation.estimated_input_tokens},
        "output": {"max_output_tokens": allocation.output_token_limit},
        "cost": {"max_estimated_cost_usd": allocation.reserved_cost_usd},
        "count": {},
    }[limit]
    _, claim, _, _, attempts, store = await prepare(
        database_session_factory,
        CallBudgetPolicy(max_model_calls=1 if limit == "count" else 12),
        model_budget=model_policy(**limits),
    )
    arguments = {
        "operation": CallOperation.ANALYZE_LOG,
        "model_input": model_input(),
        "prompt_version": "test-v1",
    }
    results = await asyncio.gather(
        *(reserve_slot(store, claim, attempts["log"], i, **arguments) for i in (1, 2)),
        return_exceptions=True,
    )
    assert sum(isinstance(r, CallBudgetExceeded) for r in results) == 1
    original = next(r for r in results if isinstance(r, CallGrant))
    index = original.record.invocation_index
    assert not (await reserve_slot(store, claim, attempts["log"], index, **arguments)).created
    with pytest.raises(TaskFactConflict, match="另一份"):
        await reserve_slot(
            store,
            claim,
            attempts["log"],
            index,
            **{**arguments, "model_input": model_input("不同输入")},
        )
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert budget.reserved_model_calls == 1
    assert budget.model_usage.unknown_model_calls == 1


async def test_runner_executes_each_slot_once_and_checks_before_invoking(database_session_factory):
    _, claim, _, _, attempts, store = await prepare(
        database_session_factory, CallBudgetPolicy(max_tool_calls_per_task=2)
    )
    log = attempts["log"]
    runner = BudgetedCallRunner(
        run_id=claim.run.id, lease_token=claim.work_item.lease_token, store=store
    )
    invoked = 0

    async def invoke():
        nonlocal invoked
        invoked += 1
        return invoked

    with runner.task_scope(log.task_id, log.id):
        assert await runner.execute(CallOperation.QUERY_LOGS, invoke) == 1
        assert await runner.execute(CallOperation.QUERY_LOGS, invoke, invocation_index=1) == 2
        with pytest.raises(TaskFactConflict, match="相同预留"):
            await runner.execute(CallOperation.QUERY_LOGS, invoke, invocation_index=1)
        with pytest.raises(CallBudgetExceeded):
            await runner.execute(CallOperation.QUERY_LOGS, invoke, invocation_index=2)
    assert invoked == 2
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert [c.invocation_index for c in budget.calls] == [0, 1]
    assert all(c.status == "succeeded" for c in budget.calls)


async def test_crash_nonzero_slot_keeps_charge_across_attempts(database_session_factory):
    parent, claim, clock, tasks, attempts, store = await prepare(
        database_session_factory, CallBudgetPolicy(max_tool_calls_per_task=1)
    )
    original = await reserve_slot(store, claim, attempts["log"], 2)
    await parent.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="模拟调用预留后崩溃"),
        retry_delay=timedelta(0),
    )
    clock.current += timedelta(seconds=1)
    replacement = await parent.claim_next(
        worker_id="replacement", occurred_at=clock(), lease_duration=timedelta(seconds=30)
    )
    snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
    log = next(t for t in snapshot.rounds[0].tasks if t.assignment.role == "log")
    retry = await tasks.begin_attempt(
        claim.run.id,
        log.id,
        lease_token=replacement.work_item.lease_token,
        expected_version=log.version,
    )
    with pytest.raises(CallBudgetExceeded) as caught:
        await reserve_slot(store, replacement, retry.attempt, 0)
    assert caught.value.code == "task_tool_budget_exceeded"
    for token in (claim.work_item.lease_token, replacement.work_item.lease_token):
        with pytest.raises(TaskFactConflict):
            await store.settle(
                claim.run.id, original.record.id, lease_token=token, status="succeeded"
            )
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert budget.reserved_tool_calls == 1
    assert budget.calls[0].status == "unknown" and budget.calls[0].invocation_index == 2


@pytest.mark.parametrize("index", [-1, 100, True, "1", 1.0, None])
async def test_invalid_index_does_not_create_reservation(database_session_factory, index):
    _, claim, _, _, attempts, store = await prepare(database_session_factory)
    with pytest.raises(ValidationError):
        await reserve_slot(store, claim, attempts["log"], index)
    assert (
        await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    ).calls == ()
