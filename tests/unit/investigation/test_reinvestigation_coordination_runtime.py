"""纯假端口验证回流协调 Worker 的外部调用、复用和迟到结果边界。"""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx2
import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from opsswarm.investigation.budget.model_contracts import ModelInput, TextModelMessage
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.coordination.contracts import CoordinationProposal, ProposedTask
from opsswarm.investigation.reinvestigation_coordination.contracts import (
    ReflowClaim,
    ReflowError,
    ReflowModelResult,
)
from opsswarm.investigation.reinvestigation_coordination.job_store import _require_lease
from opsswarm.investigation.reinvestigation_coordination.ledger import _validate_frozen_call
from opsswarm.investigation.reinvestigation_coordination.model import ReflowModel
from opsswarm.investigation.reinvestigation_coordination.worker import ReflowWorker
from tests.model_budget_helpers import model_policy, sdk_model, sdk_response
from tests.unit.investigation.test_human_preparation import NOW


def proposal_result():
    return ReflowModelResult(
        proposal=CoordinationProposal(
            round_number=2,
            goal="对失败恢复继续调查",
            tasks=(
                ProposedTask(
                    task_key="review_failed_recovery",
                    role="log",
                    goal="调查恢复失败原因",
                    lookback_seconds=300,
                ),
            ),
        )
    )


def worker_fixture(*, reused=False):
    source = SimpleNamespace(model_budget=object(), investigation_run_id=uuid4())
    packet = SimpleNamespace(model_input=object())
    claim = ReflowClaim(
        job_id=uuid4(),
        investigation_run_id=source.investigation_run_id,
        attempt_id=uuid4(),
        number=1,
        lease_token=uuid4(),
        lease_expires_at=NOW + timedelta(seconds=600),
    )
    call_id = uuid4()
    store = SimpleNamespace(
        prepare_next=AsyncMock(return_value=False),
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
    client = SimpleNamespace(usage=None, invoke=AsyncMock(return_value=proposal_result()))
    factory = Mock(return_value=client)
    worker = ReflowWorker(store, ledger, model_factory=factory, worker_id="unit-candidate")
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
            result=proposal_result(),
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
        error.code if isinstance(error, CallBudgetExceeded) else "reflow_failed"
    )


@pytest.mark.asyncio
async def test_lease_loss_drops_late_result_without_settling_or_failing_new_owner():
    worker, store, ledger, client, *_ = worker_fixture()
    client.invoke.side_effect = ReflowError("reflow_lease_lost", "租约被替换")
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
        "error_code": "reflow_source_changed",
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


@pytest.mark.asyncio
async def test_discovered_blocked_stage_counts_as_work_without_model_request():
    worker, store, ledger, client, factory, *_ = worker_fixture()
    store.prepare_next.return_value = True
    store.claim_next.return_value = None
    assert await worker.run_once() is True
    store.prepare_next.assert_awaited_once()
    ledger.reserve.assert_not_awaited()
    factory.assert_not_called()


@pytest.mark.asyncio
async def test_successful_call_then_completion_failure_retries_without_resettling_call():
    worker, store, ledger, client, _, claim, _, call_id = worker_fixture(reused=True)
    store.complete.side_effect = RuntimeError("写工件失败")
    assert await worker.run_once() is True
    ledger.settle.assert_not_awaited()
    client.invoke.assert_not_awaited()
    store.fail.assert_awaited_once_with(claim, error_code="reflow_failed", retryable=True)


def frozen_input():
    return ModelInput(
        messages=(
            TextModelMessage(role="system", content="只建议调查分工"),
            TextModelMessage(role="user", content='{"约束":"保留反证和人工限制"}'),
        ),
        output_schema=CoordinationProposal.model_json_schema(),
        tool_schema=convert_to_openai_tool(CoordinationProposal),
        structured_output_method="function_calling",
    )


def frozen_policy():
    policy = model_policy()
    return policy.model_copy(
        update={
            "profile": policy.profile.model_copy(
                update={"structured_output_method": "function_calling"}
            )
        }
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_mode", ["reported", "missing", "partial", "null"])
async def test_sdk_uses_exact_frozen_schema_and_keeps_usage_unknown(usage_mode):
    supplied, requests = frozen_input(), []

    def reply(request):
        payload = json.loads(request.content)
        requests.append(payload)
        response = sdk_response(
            payload,
            proposal_result().proposal.model_dump(mode="json"),
            usage=usage_mode != "missing",
        )
        body = response.json()
        if usage_mode == "partial":
            body["usage"] = {"prompt_tokens": 80}
        if usage_mode == "null":
            body["usage"]["total_tokens"] = None
        return httpx2.Response(200, json=body)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client:
        caller = ReflowModel(sdk_model(client), frozen_policy())
        assert await caller.invoke(supplied) == proposal_result()
        assert len(requests) == 1
        assert requests[0]["messages"] == [m.model_dump() for m in supplied.messages]
        assert requests[0]["tools"] == [supplied.tool_schema]
        assert requests[0]["max_completion_tokens"] == 4096
        if usage_mode == "reported":
            assert caller.usage.total_tokens == 100
        else:
            assert caller.usage is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,code",
    [
        ({"output_schema": {}}, "model_input_schema_mismatch"),
        ({"tool_schema": {}}, "model_input_schema_mismatch"),
        ({"structured_output_method": "json_schema"}, "model_budget_profile_mismatch"),
    ],
)
async def test_invalid_frozen_input_never_sends_request(changes, code):
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("无效输入不得请求模型"))
    ) as client:
        caller = ReflowModel(sdk_model(client), frozen_policy())
        with pytest.raises(CallBudgetExceeded) as caught:
            await caller.invoke(frozen_input().model_copy(update=changes))
        assert caught.value.code == code
        assert caller.usage is None


@pytest.mark.parametrize(
    "change",
    [
        {"status": "queued"},
        {"lease_token": uuid4()},
        {"locked_until": None},
        {"locked_until": NOW},
    ],
)
def test_expired_or_changed_lease_rejects_late_result(change):
    token = uuid4()
    values = {
        "status": "running",
        "lease_token": token,
        "locked_until": NOW + timedelta(seconds=120),
    } | change
    with pytest.raises(ReflowError) as caught:
        _require_lease(SimpleNamespace(**values), token, NOW)
    assert caught.value.code == "reflow_lease_lost"


@pytest.mark.parametrize(
    "change",
    [
        {"reinvestigation_attempt_id": uuid4()},
        {"investigation_run_id": uuid4()},
        {"investigation_attempt_id": uuid4()},
        {"candidate_attempt_id": uuid4()},
        {"human_review_attempt_id": uuid4()},
        {"tool_request": {}},
        {"task_id": uuid4()},
        {"task_attempt_id": uuid4()},
        {"invocation_index": 1},
        {"operation": "coordinate_round"},
        {"idempotency_key": "reflow:other"},
        {"model_input": {}},
        {"model_allocation": {}},
        {"context_manifest": {}},
    ],
)
def test_own_reservation_requires_exact_attempt_scope_and_frozen_input(change):
    _, _, _, _, _, claim, _, _ = worker_fixture()
    source = SimpleNamespace(parent_attempt_id=uuid4())
    packet = SimpleNamespace(
        model_input=Mock(model_dump=Mock(return_value={"frozen": "input"})),
        allocation=Mock(model_dump=Mock(return_value={"frozen": "allocation"})),
        manifest=Mock(model_dump=Mock(return_value={"frozen": "manifest"})),
    )
    values = {
        "investigation_run_id": claim.investigation_run_id,
        "reinvestigation_attempt_id": claim.attempt_id,
        "investigation_attempt_id": source.parent_attempt_id,
        "candidate_attempt_id": None,
        "human_review_attempt_id": None,
        "tool_request": None,
        "task_id": None,
        "task_attempt_id": None,
        "operation": "coordinate_reinvestigation",
        "kind": "model",
        "invocation_index": 0,
        "idempotency_key": f"reflow:{claim.attempt_id}",
        "model_input": {"frozen": "input"},
        "model_allocation": {"frozen": "allocation"},
        "context_manifest": {"frozen": "manifest"},
    }
    _validate_frozen_call(SimpleNamespace(**values), claim, source, packet)
    with pytest.raises(ReflowError) as caught:
        _validate_frozen_call(SimpleNamespace(**(values | change)), claim, source, packet)
    assert caught.value.code == "reflow_call_invalid"
