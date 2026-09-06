"""真实 PostgreSQL 的 Token/费用原子预留、恢复和超支事实。"""

import asyncio
from datetime import timedelta

import pytest

from opsswarm.investigation.budget.contracts import CallGrant, CallOperation
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.budget.model_policy import allocate_input
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.models import WorkItemError
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries
from tests.integration.test_call_budget_store import prepare
from tests.model_budget_helpers import model_input, model_policy

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def reserve_model(store, claim, attempt, inputs=None):
    return await store.reserve(
        claim.run.id,
        lease_token=claim.work_item.lease_token,
        operation=CallOperation.ANALYZE_LOG,
        task_id=attempt.task_id,
        task_attempt_id=attempt.id,
        model_input=inputs or model_input(),
        prompt_version="test-v1",
    )


@pytest.mark.parametrize("limit", ["input", "output", "cost"])
async def test_parallel_model_reservations_share_last_capacity(database_session_factory, limit):
    allocation = allocate_input(model_policy(), model_input(), prompt_version="test-v1")
    limits = {
        "input": {"max_input_tokens": allocation.estimated_input_tokens},
        "output": {"max_output_tokens": allocation.output_token_limit},
        "cost": {"max_estimated_cost_usd": allocation.reserved_cost_usd},
    }[limit]
    _, claim, _, _, attempts, store = await prepare(
        database_session_factory, model_budget=model_policy(**limits)
    )
    results = await asyncio.gather(
        *(
            store.reserve(
                claim.run.id,
                lease_token=claim.work_item.lease_token,
                operation=CallOperation(f"analyze_{role}"),
                task_id=attempt.task_id,
                task_attempt_id=attempt.id,
                model_input=model_input(),
                prompt_version="test-v1",
            )
            for role, attempt in attempts.items()
        ),
        return_exceptions=True,
    )
    assert sum(isinstance(r, CallGrant) for r in results) == 1
    assert sum(isinstance(r, CallBudgetExceeded) for r in results) == 2
    snapshot = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert snapshot.reserved_model_calls == 1 and len(snapshot.calls) == 1
    assert snapshot.model_usage.unknown_model_calls == 1
    assert snapshot.model_usage.reported_cost_usd is None


async def test_input_required_and_replay_cannot_change_manifest(database_session_factory):
    _, claim, _, _, attempts, store = await prepare(
        database_session_factory, model_budget=model_policy()
    )
    log = attempts["log"]
    with pytest.raises(CallBudgetExceeded):
        await store.reserve(
            claim.run.id,
            lease_token=claim.work_item.lease_token,
            operation=CallOperation.SYNTHESIZE,
        )
    grant = await reserve_model(store, claim, log)
    assert not (await reserve_model(store, claim, log)).created
    with pytest.raises(TaskFactConflict, match="另一份"):
        await reserve_model(store, claim, log, model_input("不同输入"))
    queries = CallBudgetQueries(database_session_factory)
    assert await queries.get_model_input(claim.run.id, grant.record.id) == model_input()
    assert (await queries.get_snapshot(claim.run.id)).reserved_model_calls == 1
    _, other, _, _, _, _ = await prepare(database_session_factory, model_budget=model_policy())
    with pytest.raises(TaskFactConflict, match="指定调用"):
        await queries.get_model_input(other.run.id, grant.record.id)


async def test_unknown_charge_survives_parent_retry_and_late_usage_is_rejected(
    database_session_factory,
):
    parent, claim, clock, tasks, attempts, store = await prepare(
        database_session_factory, model_budget=model_policy(max_output_tokens=4096)
    )
    grant = await reserve_model(store, claim, attempts["log"])
    await parent.fail(
        claim.work_item.id,
        lease_token=claim.work_item.lease_token,
        occurred_at=clock(),
        error=WorkItemError(error_code="crash", message="模拟模型预留后失联"),
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
        await reserve_model(store, replacement, attempt.attempt)
    for token in (claim.work_item.lease_token, replacement.work_item.lease_token):
        with pytest.raises(TaskFactConflict):
            await store.settle(
                claim.run.id,
                grant.record.id,
                lease_token=token,
                status="succeeded",
                model_usage=ReportedModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            )
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert budget.calls[0].status == "unknown" and budget.calls[0].model_usage is None
    assert budget.model_usage.committed_output_tokens == 4096


async def test_usage_overrun_is_saved_before_subsequent_calls_stop(database_session_factory):
    _, claim, _, _, attempts, store = await prepare(
        database_session_factory, model_budget=model_policy()
    )
    grant = await reserve_model(store, claim, attempts["log"])
    usage = ReportedModelUsage(input_tokens=999999, output_tokens=999999, total_tokens=1999998)
    await store.settle(
        claim.run.id,
        grant.record.id,
        lease_token=claim.work_item.lease_token,
        status="succeeded",
        model_usage=usage,
    )
    with pytest.raises(CallBudgetExceeded):
        await store.check(claim.run.id, lease_token=claim.work_item.lease_token)
    result = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert result.calls[0].model_usage == usage
    assert result.model_usage.committed_input_tokens == usage.input_tokens
    assert result.model_usage.reported_cost_usd > 1


@pytest.mark.parametrize("invalid_json", [False, True])
async def test_sdk_parse_failure_still_persists_reported_usage(
    database_session_factory, invalid_json
):
    import httpx2
    from langchain_core.messages import SystemMessage

    from opsswarm.investigation.budget.model_caller import BudgetedModelCaller
    from opsswarm.investigation.budget.runtime import BudgetedCallRunner
    from tests.model_budget_helpers import sdk_model, sdk_response
    from tests.unit.investigation.test_budgeted_model_caller import Answer

    policy = model_policy()
    _, claim, _, _, attempts, store = await prepare(database_session_factory, model_budget=policy)
    runner = BudgetedCallRunner(
        run_id=claim.run.id, lease_token=claim.work_item.lease_token, store=store
    )
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(
            lambda _: sdk_response({}, {"answer": "不是整数"}, invalid_json=invalid_json)
        )
    ) as client:
        caller = BudgetedModelCaller(sdk_model(client), policy, runner)
        log = attempts["log"]
        with runner.task_scope(log.task_id, log.id), pytest.raises(ValueError):
            await caller.invoke(
                Answer, [SystemMessage(content="返回 JSON")], CallOperation.ANALYZE_LOG
            )
    result = await CallBudgetQueries(database_session_factory).get_snapshot(claim.run.id)
    assert result.calls[0].status == "failed"
    assert result.calls[0].model_usage.total_tokens == 100
    assert result.model_usage.unknown_model_calls == 0
    assert result.model_usage.committed_output_tokens == 4096
