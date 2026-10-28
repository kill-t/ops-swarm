"""用纯预算事实和假端口验证独立专业执行的并发、恢复与迟到边界。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from opsswarm.investigation.budget.contracts import CallReservationRecord
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.budget.model_policy import allocate_input
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_execution.contracts import (
    ANALYZE,
    COLLECT,
    ExecutionClaim,
)
from opsswarm.investigation.reinvestigation_execution.policy import check_budget
from opsswarm.investigation.reinvestigation_execution.worker import ExecutionWorker
from tests.model_budget_helpers import model_input
from tests.unit.investigation.test_reflow_context_policy import NOW, budget_snapshot, reflow_source


def allocation_for(source):
    return allocate_input(
        source.model_budget,
        model_input(method="function_calling"),
        prompt_version="reinvestigation-specialist-v1",
    )


def task_call(source, identity, *, sequence=1, operation=ANALYZE, status="reserved", **changes):
    return CallReservationRecord(
        **(
            {
                "id": uuid4(),
                "investigation_run_id": source.investigation_run_id,
                "investigation_attempt_id": source.parent_attempt_id,
                "reflow_task_id": identity,
                "reflow_task_attempt_id": uuid4(),
                "task_id": None,
                "task_attempt_id": None,
                "sequence": sequence,
                "operation": operation,
                "kind": "model" if operation == ANALYZE else "tool",
                "status": status,
                "error_code": "operation_cancelled" if status in ("unknown", "failed") else None,
                "reserved_at": NOW - timedelta(seconds=1),
                "finished_at": None if status == "reserved" else NOW,
                "model_allocation": allocation_for(source) if operation == ANALYZE else None,
            }
            | changes
        )
    )


def assert_budget_error(code, source, calls, **changes):
    with pytest.raises(CallBudgetExceeded) as caught:
        check_budget(
            source,
            budget_snapshot(source, calls),
            task_id=calls[-1].reflow_task_id if calls else uuid4(),
            **({"now": NOW} | changes),
        )
    assert caught.value.code == code


def test_three_pending_professionals_share_original_exact_capacity_without_self_blocking():
    source = reflow_source()
    allocation = allocation_for(source)
    source = source.model_copy(
        update={
            "call_budget": source.call_budget.model_copy(update={"max_model_calls": 3}),
            "model_budget": source.model_budget.model_copy(
                update={
                    "max_input_tokens": 3 * allocation.estimated_input_tokens,
                    "max_output_tokens": 3 * allocation.output_token_limit,
                    "max_estimated_cost_usd": 3 * allocation.reserved_cost_usd,
                }
            ),
        }
    )
    identities = [uuid4() for _ in range(3)]
    calls = tuple(
        task_call(source, identity, sequence=i + 1) for i, identity in enumerate(identities)
    )
    # 第三专业在前两个真实在途预留之后按单次增量获得额度。
    check_budget(
        source,
        budget_snapshot(source, calls[:2]),
        task_id=identities[2],
        now=NOW,
        reserve=ANALYZE,
        allocation=allocation,
    )
    for identity in identities:
        check_budget(source, budget_snapshot(source, calls), task_id=identity, now=NOW)
    usage = budget_snapshot(source, calls).model_usage
    assert usage.unknown_model_calls == 3
    assert usage.committed_input_tokens == 3 * allocation.estimated_input_tokens
    assert usage.committed_output_tokens == 3 * allocation.output_token_limit
    assert_budget_error(
        "model_call_budget_exceeded", source, calls, reserve=ANALYZE, allocation=allocation
    )


def test_each_reflow_task_gets_its_own_tool_limit_and_attempts_do_not_reset_it():
    source = reflow_source()
    source = source.model_copy(
        update={"call_budget": source.call_budget.model_copy(update={"max_tool_calls_per_task": 2})}
    )
    first, second = uuid4(), uuid4()
    calls = (
        task_call(source, first, sequence=1, operation=COLLECT, status="unknown"),
        task_call(source, first, sequence=2, operation=COLLECT, status="failed"),
    )
    assert calls[0].reflow_task_attempt_id != calls[1].reflow_task_attempt_id
    assert_budget_error("tool_call_budget_exceeded", source, calls, reserve=COLLECT)
    check_budget(source, budget_snapshot(source, calls), task_id=second, now=NOW, reserve=COLLECT)
    calls += (task_call(source, second, sequence=3, operation=COLLECT, status="succeeded"),)
    check_budget(source, budget_snapshot(source, calls), task_id=second, now=NOW, reserve=COLLECT)


def test_legacy_and_reflow_tool_identities_are_counted_separately():
    source = reflow_source()
    source = source.model_copy(
        update={"call_budget": source.call_budget.model_copy(update={"max_tool_calls_per_task": 1})}
    )
    old_id, new_id = uuid4(), uuid4()
    old = task_call(
        source,
        old_id,
        status="succeeded",
        task_id=old_id,
        task_attempt_id=uuid4(),
        reflow_task_id=None,
        reflow_task_attempt_id=None,
        operation="query_logs",
    )
    assert old.scope_task_id == old_id
    check_budget(source, budget_snapshot(source, (old,)), task_id=new_id, now=NOW, reserve=COLLECT)
    with pytest.raises(CallBudgetExceeded):
        check_budget(
            source, budget_snapshot(source, (old,)), task_id=old_id, now=NOW, reserve=COLLECT
        )


@pytest.mark.parametrize("status", ["reserved", "unknown", "failed", "succeeded"])
def test_missing_usage_keeps_full_allocation_after_every_call_status(status):
    source = reflow_source()
    identity = uuid4()
    call = task_call(source, identity, status=status)
    source = source.model_copy(
        update={
            "model_budget": source.model_budget.model_copy(
                update={"max_input_tokens": call.model_allocation.estimated_input_tokens}
            )
        }
    )
    snapshot = budget_snapshot(source, (call,))
    check_budget(source, snapshot, task_id=identity, now=NOW)
    assert snapshot.model_usage.unknown_model_calls == 1
    assert snapshot.model_usage.reported_cost_usd is None
    assert (
        snapshot.model_usage.committed_input_tokens == call.model_allocation.estimated_input_tokens
    )
    assert_budget_error(
        "run_input_budget_exceeded",
        source,
        (call,),
        reserve=ANALYZE,
        allocation=allocation_for(source),
    )


@pytest.mark.parametrize("over", ["input", "output"])
def test_actual_overuse_remains_audit_usage_and_blocks_publication(over):
    source = reflow_source()
    allocation = allocation_for(source)
    inputs = allocation.estimated_input_tokens + int(over == "input")
    outputs = allocation.output_token_limit + int(over == "output")
    call = task_call(
        source,
        uuid4(),
        status="unknown",
        model_usage=ReportedModelUsage(
            input_tokens=inputs, output_tokens=outputs, total_tokens=inputs + outputs
        ),
    )
    assert_budget_error("model_usage_exceeded_allocation", source, (call,))
    usage = budget_snapshot(source, (call,)).model_usage
    assert usage.reported_input_tokens == inputs
    assert usage.reported_output_tokens == outputs


@pytest.mark.parametrize("offset", [0, 1])
def test_original_deadline_is_not_extended_for_new_task_or_attempt(offset):
    source = reflow_source()
    now = source.started_at + timedelta(seconds=source.call_budget.max_duration_seconds + offset)
    assert_budget_error("run_deadline_exceeded", source, (), now=now, reserve=COLLECT)


def test_unrelated_old_inflight_call_cannot_be_treated_as_professional_parallel_call():
    source = reflow_source()
    call = task_call(source, uuid4(), reflow_task_id=None, reflow_task_attempt_id=None)
    assert_budget_error("calls_in_flight", source, (call,))


@pytest.mark.parametrize(
    "field", ["reserved_model_calls", "model_usage", "started_at", "deadline", "calls"]
)
def test_corrupt_cumulative_facts_are_rejected_before_reservation(field):
    source = reflow_source()
    call = task_call(source, uuid4())
    budget = budget_snapshot(source, (call,))
    changes = {
        "reserved_model_calls": 0,
        "model_usage": budget.model_usage.model_copy(update={"unknown_model_calls": 0}),
        "started_at": NOW,
        "deadline": NOW + timedelta(seconds=900),
        "calls": (call.model_copy(update={"sequence": 2}),),
    }
    with pytest.raises(ReflowError) as caught:
        check_budget(
            source, budget.model_copy(update={field: changes[field]}), task_id=uuid4(), now=NOW
        )
    assert caught.value.code == "execution_integrity_violation"


@pytest.mark.parametrize(
    "limit,code",
    [
        ("max_output_tokens", "run_output_budget_exceeded"),
        ("max_estimated_cost_usd", "model_cost_budget_exceeded"),
    ],
)
def test_other_roles_and_retries_share_output_and_estimated_cost_limits(limit, code):
    source = reflow_source()
    allocation = allocation_for(source)
    value = (
        allocation.output_token_limit
        if limit == "max_output_tokens"
        else allocation.reserved_cost_usd
    )
    source = source.model_copy(
        update={"model_budget": source.model_budget.model_copy(update={limit: value})}
    )
    call = task_call(source, uuid4(), status="unknown")
    assert_budget_error(code, source, (call,), reserve=ANALYZE, allocation=allocation)


def worker_fixture(*, reuse_evidence=False, reuse_model=False):
    root, identity = uuid4(), uuid4()
    claim = ExecutionClaim(
        task_id=identity,
        investigation_run_id=root,
        attempt_id=uuid4(),
        number=1,
        lease_token=uuid4(),
        lease_expires_at=NOW + timedelta(seconds=120),
    )
    source = SimpleNamespace(source=SimpleNamespace(model_budget=object()), assignment=object())
    window, snapshot, inputs, result = object(), object(), object(), object()
    evidence_call, model_call = uuid4(), uuid4()
    store = SimpleNamespace(
        prepare_next=AsyncMock(return_value=False),
        claim_next=AsyncMock(return_value=claim),
        context=AsyncMock(return_value=(source, window)),
        complete=AsyncMock(),
        fail=AsyncMock(),
    )
    ledger = SimpleNamespace(
        reserve=AsyncMock(
            side_effect=[
                SimpleNamespace(
                    record=SimpleNamespace(id=evidence_call), created=not reuse_evidence
                ),
                SimpleNamespace(record=SimpleNamespace(id=model_call), created=not reuse_model),
            ]
        ),
        check=AsyncMock(return_value=1),
        save_evidence=AsyncMock(),
        prepare_packet=AsyncMock(return_value=SimpleNamespace(model_input=inputs)),
        settle=AsyncMock(return_value=True),
    )
    evidence = SimpleNamespace(collect=AsyncMock(return_value=snapshot))
    usage = ReportedModelUsage(input_tokens=80, output_tokens=20, total_tokens=100)
    model = SimpleNamespace(usage=usage, invoke=AsyncMock(return_value=result))
    factory = Mock(return_value=model)
    worker = ExecutionWorker(
        store, ledger, evidence, model_factory=factory, worker_id="unit-professional"
    )
    return SimpleNamespace(
        worker=worker,
        store=store,
        ledger=ledger,
        evidence=evidence,
        client=model,
        factory=factory,
        claim=claim,
        source=source,
        window=window,
        snapshot=snapshot,
        inputs=inputs,
        result=result,
        evidence_call=evidence_call,
        model_call=model_call,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reuse_evidence,reuse_model", [(False, False), (True, False), (True, True)]
)
async def test_successful_saved_evidence_and_model_are_reused_without_repeating_external_calls(
    reuse_evidence, reuse_model
):
    f = worker_fixture(reuse_evidence=reuse_evidence, reuse_model=reuse_model)
    assert await f.worker.run_once() is True
    assert f.ledger.reserve.await_args_list[0].args == (f.claim, COLLECT)
    assert f.ledger.reserve.await_args_list[1].args == (f.claim, ANALYZE)
    f.store.complete.assert_awaited_once_with(f.claim, f.model_call)
    f.store.fail.assert_not_awaited()
    if reuse_evidence:
        f.evidence.collect.assert_not_awaited()
        f.ledger.save_evidence.assert_not_awaited()
    else:
        f.evidence.collect.assert_awaited_once_with(f.source.assignment, f.claim.task_id, f.window)
        f.ledger.save_evidence.assert_awaited_once_with(f.claim, f.evidence_call, f.snapshot)
    if reuse_model:
        f.client.invoke.assert_not_awaited()
        f.ledger.settle.assert_not_awaited()
    else:
        f.client.invoke.assert_awaited_once_with(f.inputs)
        f.ledger.settle.assert_awaited_once_with(
            f.claim, f.model_call, status="succeeded", result=f.result, usage=f.client.usage
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("prepared", [False, True])
async def test_no_claim_never_calls_sources_or_models(prepared):
    f = worker_fixture()
    f.store.prepare_next.return_value = prepared
    f.store.claim_next.return_value = None
    assert await f.worker.run_once() is prepared
    f.factory.assert_not_called()
    f.ledger.reserve.assert_not_awaited()
    f.evidence.collect.assert_not_awaited()


@pytest.mark.asyncio
async def test_configuration_is_validated_before_spending_any_call_budget():
    f = worker_fixture()
    f.factory.side_effect = CallBudgetExceeded("model_budget_profile_mismatch")
    assert await f.worker.run_once() is True
    f.ledger.reserve.assert_not_awaited()
    f.store.fail.assert_awaited_once_with(
        f.claim, error_code="model_budget_profile_mismatch", retryable=False
    )


@pytest.mark.asyncio
async def test_changed_source_during_model_request_rejects_publish_without_resettling():
    f = worker_fixture(reuse_evidence=True)
    f.ledger.settle.return_value = False
    assert await f.worker.run_once() is True
    assert f.ledger.settle.await_count == 1
    f.store.complete.assert_not_awaited()
    f.store.fail.assert_awaited_once_with(
        f.claim, error_code="execution_result_rejected", retryable=False
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["evidence", "model", "publish"])
async def test_lease_loss_never_settles_or_fails_a_replacement_owner(phase):
    f = worker_fixture(reuse_evidence=phase != "evidence", reuse_model=phase == "publish")
    loss = ReflowError("execution_lease_lost", "真实租约已被替换")
    if phase == "evidence":
        f.evidence.collect.side_effect = loss
    elif phase == "model":
        f.client.invoke.side_effect = loss
    else:
        f.store.complete.side_effect = loss
    assert await f.worker.run_once() is True
    f.ledger.settle.assert_not_awaited()
    f.store.fail.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["evidence", "model"])
async def test_timeout_records_unknown_with_only_current_phase_usage(phase):
    f = worker_fixture(reuse_evidence=phase == "model")
    (f.evidence.collect if phase == "evidence" else f.client.invoke).side_effect = TimeoutError()
    assert await f.worker.run_once() is True
    f.ledger.settle.assert_awaited_once_with(
        f.claim,
        f.evidence_call if phase == "evidence" else f.model_call,
        status="unknown",
        usage=None if phase == "evidence" else f.client.usage,
    )
    f.store.complete.assert_not_awaited()
    f.store.fail.assert_awaited_once_with(f.claim, error_code="execution_timeout", retryable=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["evidence", "model"])
async def test_cancellation_audits_unknown_and_is_propagated_to_worker_shutdown(phase):
    f = worker_fixture(reuse_evidence=phase == "model")
    (
        f.evidence.collect if phase == "evidence" else f.client.invoke
    ).side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await f.worker.run_once()
    f.ledger.settle.assert_awaited_once_with(
        f.claim,
        f.evidence_call if phase == "evidence" else f.model_call,
        status="unknown",
        usage=None if phase == "evidence" else f.client.usage,
    )
    f.store.complete.assert_not_awaited()
    assert f.store.fail.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,code,retryable,status",
    [
        (ValueError("供应商自由错误文本"), "execution_failed", True, "failed"),
        (CallBudgetExceeded("model_usage_invalid"), "model_usage_invalid", False, "unknown"),
        (
            ReflowError("reflow_source_changed", "事故已变化"),
            "reflow_source_changed",
            False,
            "failed",
        ),
    ],
)
async def test_failures_preserve_stable_error_codes_without_publishing(
    error, code, retryable, status
):
    f = worker_fixture(reuse_evidence=True)
    f.client.invoke.side_effect = error
    assert await f.worker.run_once() is True
    f.ledger.settle.assert_awaited_once_with(
        f.claim, f.model_call, status=status, usage=f.client.usage
    )
    f.store.complete.assert_not_awaited()
    f.store.fail.assert_awaited_once_with(f.claim, error_code=code, retryable=retryable)


@pytest.mark.asyncio
async def test_lease_lost_during_failure_settlement_does_not_fail_new_owner():
    f = worker_fixture(reuse_evidence=True)
    f.client.invoke.side_effect = TimeoutError()
    f.ledger.settle.side_effect = ReflowError("execution_lease_lost", "结算时已失租")
    assert await f.worker.run_once() is True
    f.store.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_result_persistence_crash_retries_without_repeating_successful_model_call():
    f = worker_fixture(reuse_evidence=True, reuse_model=True)
    f.store.complete.side_effect = RuntimeError("结果写入事务失败")
    assert await f.worker.run_once() is True
    f.client.invoke.assert_not_awaited()
    f.ledger.settle.assert_not_awaited()
    f.store.fail.assert_awaited_once_with(f.claim, error_code="execution_failed", retryable=True)


@pytest.mark.asyncio
async def test_budget_failure_before_model_reservation_does_not_resettle_saved_evidence():
    f = worker_fixture(reuse_evidence=True)
    f.ledger.reserve.side_effect = [
        SimpleNamespace(record=SimpleNamespace(id=f.evidence_call), created=False),
        CallBudgetExceeded("model_call_budget_exceeded"),
    ]
    assert await f.worker.run_once() is True
    f.ledger.settle.assert_not_awaited()
    f.client.invoke.assert_not_awaited()
    f.store.complete.assert_not_awaited()
    f.store.fail.assert_awaited_once_with(
        f.claim, error_code="model_call_budget_exceeded", retryable=False
    )
