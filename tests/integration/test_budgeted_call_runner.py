"""预算入口在调用前拒绝，并如实保留超时和取消的预留。"""

import asyncio
from datetime import timedelta

import pytest

from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallOperation
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.tasks.errors import TaskFactConflict
from tests.integration.test_call_budget_store import prepare, reserve
from tests.task_facts_helpers import BASE_TIME

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_replayed_reservation_and_elapsed_deadline_never_invoke(database_session_factory):
    _, claim, clock, _, attempts, store = await prepare(
        database_session_factory, CallBudgetPolicy(max_duration_seconds=3)
    )
    log = attempts["log"]
    await reserve(store, claim, log, CallOperation.QUERY_LOGS)
    runner = BudgetedCallRunner(
        run_id=claim.run.id, lease_token=claim.work_item.lease_token, store=store
    )
    calls = 0

    async def invoke():
        nonlocal calls
        calls += 1

    with runner.task_scope(log.task_id, log.id):
        with pytest.raises(TaskFactConflict, match="相同预留"):
            await runner.execute(CallOperation.QUERY_LOGS, invoke)
        clock.current = BASE_TIME + timedelta(seconds=5)
        with pytest.raises(CallBudgetExceeded):
            await runner.execute(CallOperation.ANALYZE_LOG, invoke)
    assert calls == 0


@pytest.mark.parametrize("mode", ["deadline", "inner_timeout", "cancel"])
async def test_uncertain_calls_stay_charged(database_session_factory, mode):
    _, claim, clock, _, attempts, store = await prepare(
        database_session_factory, CallBudgetPolicy(max_duration_seconds=2)
    )
    clock.current = BASE_TIME + timedelta(seconds=3.8)
    log = attempts["log"]
    runner = BudgetedCallRunner(
        run_id=claim.run.id, lease_token=claim.work_item.lease_token, store=store
    )

    async def invoke():
        if mode == "inner_timeout":
            raise TimeoutError("模拟供应商超时")
        if mode == "cancel":
            raise asyncio.CancelledError()
        await asyncio.sleep(10)

    expected = {
        "deadline": CallBudgetExceeded,
        "inner_timeout": TimeoutError,
        "cancel": asyncio.CancelledError,
    }[mode]
    with runner.task_scope(log.task_id, log.id), pytest.raises(expected):
        await runner.execute(CallOperation.ANALYZE_LOG, invoke)
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert budget.reserved_model_calls == 1 and budget.calls[0].status == "unknown"
    assert (
        budget.calls[0].error_code
        == {
            "deadline": "run_deadline_exceeded",
            "inner_timeout": "operation_failed",
            "cancel": "operation_cancelled",
        }[mode]
    )
