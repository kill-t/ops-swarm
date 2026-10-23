"""纯假端口验证候选 Worker 的外部调用、复用和迟到结果边界。"""

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest

from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.candidate.context import build_packet
from opsswarm.investigation.candidate.contracts import CandidateClaim, CandidateError
from opsswarm.investigation.candidate.worker import CandidateWorker
from tests.unit.investigation.test_candidate_evaluation import candidate, candidate_source
from tests.unit.investigation.test_human_preparation import NOW


def worker_fixture(*, reused=False):
    source = candidate_source()
    packet = build_packet(source)
    claim = CandidateClaim(
        job_id=uuid4(),
        investigation_run_id=source.handoff.investigation_run_id,
        attempt_id=uuid4(),
        number=1,
        lease_token=uuid4(),
        lease_expires_at=NOW + timedelta(seconds=600),
    )
    call_id = uuid4()
    store = SimpleNamespace(
        claim_next=AsyncMock(return_value=claim),
        get_packet=AsyncMock(return_value=(source, packet)),
        complete=AsyncMock(),
        fail=AsyncMock(),
    )
    ledger = SimpleNamespace(
        reserve=AsyncMock(
            return_value=SimpleNamespace(
                created=not reused,
                record=SimpleNamespace(id=call_id),
            )
        ),
        check=AsyncMock(return_value=1),
        settle=AsyncMock(return_value=True),
    )
    client = SimpleNamespace(usage=None, invoke=AsyncMock(return_value=candidate()))
    factory = Mock(return_value=client)
    worker = CandidateWorker(store, ledger, model_factory=factory, worker_id="unit-candidate")
    return worker, store, ledger, client, factory, claim, packet, call_id


@pytest.mark.asyncio
@pytest.mark.parametrize("reused", [False, True])
async def test_worker_uses_frozen_packet_or_reuses_success_without_model_request(reused):
    worker, store, ledger, client, factory, claim, packet, call_id = worker_fixture(reused=reused)
    assert await worker.run_once() is True
    store.complete.assert_awaited_once_with(claim, call_id)
    store.fail.assert_not_awaited()
    factory.assert_called_once()
    if reused:
        ledger.check.assert_not_awaited()
        ledger.settle.assert_not_awaited()
        client.invoke.assert_not_awaited()
    else:
        client.invoke.assert_awaited_once_with(packet.model_input)
        ledger.settle.assert_awaited_once_with(
            claim,
            call_id,
            status="succeeded",
            result=candidate(),
            usage=None,
        )


@pytest.mark.asyncio
async def test_worker_does_nothing_without_a_persistent_claim():
    worker, store, ledger, client, factory, *_ = worker_fixture()
    store.claim_next.return_value = None
    assert await worker.run_once() is False
    store.get_packet.assert_not_awaited()
    ledger.reserve.assert_not_awaited()
    factory.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error,status,retryable",
    [
        (TimeoutError(), "unknown", True),
        (ValueError("错误结构"), "failed", True),
        (CallBudgetExceeded("model_usage_invalid"), "unknown", False),
        (asyncio.CancelledError(), "unknown", False),
    ],
)
async def test_worker_settles_bounded_failures_without_publishing(error, status, retryable):
    worker, store, ledger, client, _, claim, _, call_id = worker_fixture()
    client.invoke.side_effect = error
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await worker.run_once()
    else:
        assert await worker.run_once() is True
    ledger.settle.assert_awaited_once_with(claim, call_id, status=status, usage=None)
    store.complete.assert_not_awaited()
    assert store.fail.await_args.kwargs["retryable"] is retryable
    assert store.fail.await_args.kwargs["error_code"] == (
        error.code if isinstance(error, CallBudgetExceeded) else "candidate_failed"
    )


@pytest.mark.asyncio
async def test_lease_loss_drops_late_result_without_settling_or_failing_new_owner():
    worker, store, ledger, client, *_ = worker_fixture()
    client.invoke.side_effect = CandidateError("candidate_lease_lost", "租约被替换")
    assert await worker.run_once() is True
    ledger.settle.assert_not_awaited()
    store.complete.assert_not_awaited()
    store.fail.assert_not_awaited()


@pytest.mark.asyncio
async def test_source_changed_during_request_never_publishes_or_repeats_settlement():
    worker, store, ledger, client, *_ = worker_fixture()
    ledger.settle.return_value = False
    assert await worker.run_once() is True
    assert ledger.settle.await_count == 1
    store.complete.assert_not_awaited()
    assert store.fail.await_args.kwargs == {
        "error_code": "candidate_source_changed",
        "retryable": False,
    }


@pytest.mark.asyncio
async def test_profile_configuration_failure_is_checked_before_reservation():
    worker, store, ledger, client, factory, *_ = worker_fixture()
    factory.side_effect = CallBudgetExceeded("model_budget_profile_mismatch")
    assert await worker.run_once() is True
    ledger.reserve.assert_not_awaited()
    client.invoke.assert_not_awaited()
    assert store.fail.await_args.kwargs == {
        "error_code": "model_budget_profile_mismatch",
        "retryable": False,
    }
