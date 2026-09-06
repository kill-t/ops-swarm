"""通过 SDK 假 HTTP 传输验证输出限制和结构化解析边界。"""

import json

import httpx2
import pytest
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_caller import BudgetedModelCaller
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from tests.model_budget_helpers import model_policy, sdk_model, sdk_response


class Answer(BaseModel):
    answer: int


class RecordingGate:
    async def execute(self, operation, invoke, **kwargs):
        self.operation = operation
        self.inputs = kwargs["model_input"]
        try:
            return await invoke()
        finally:
            self.usage = kwargs["usage_reader"]()


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["json_schema", "json_mode", "function_calling"])
@pytest.mark.parametrize("outcome", ["success", "invalid", "unknown", "partial"])
async def test_sdk_receives_output_limit_and_raw_usage_survives_parse_failure(method, outcome):
    original = model_policy().profile
    policy = model_policy(profile=original.model_copy(update={"structured_output_method": method}))
    gate = RecordingGate()
    requests = []

    def reply(request):
        payload = json.loads(request.content)
        assert gate.operation == CallOperation.SYNTHESIZE
        requests.append(payload)
        response = sdk_response(
            payload, {"answer": 7}, usage=outcome != "unknown", invalid_json=outcome == "invalid"
        )
        if outcome == "partial":
            body = response.json()
            body["usage"] = {"prompt_tokens": 80}
            return httpx2.Response(200, json=body)
        return response

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client:
        caller = BudgetedModelCaller(sdk_model(client), policy, gate)
        messages = [SystemMessage(content="  返回 JSON  "), HumanMessage(content="问题")]
        if outcome == "invalid":
            with pytest.raises(ValueError):
                await caller.invoke(Answer, messages, CallOperation.SYNTHESIZE)
        else:
            assert (await caller.invoke(Answer, messages, CallOperation.SYNTHESIZE)).answer == 7
    assert len(requests) == 1 and requests[0]["max_completion_tokens"] == 4096
    assert requests[0]["messages"] == [m.model_dump() for m in gate.inputs.messages]
    assert gate.inputs.output_schema == Answer.model_json_schema()
    if outcome in ("unknown", "partial"):
        assert gate.usage is None
    else:
        assert gate.usage.input_tokens == 80 and gate.usage.output_tokens == 20


@pytest.mark.asyncio
async def test_long_input_is_rejected_without_reservation_or_network():
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("不应请求模型"))
    ) as client:
        gate = RecordingGate()
        caller = BudgetedModelCaller(sdk_model(client), model_policy(), gate)
        with pytest.raises(CallBudgetExceeded):
            await caller.invoke(
                Answer, [SystemMessage(content="长输入" * 10000)], CallOperation.SYNTHESIZE
            )
    assert not hasattr(gate, "operation")


@pytest.mark.asyncio
async def test_sdk_cannot_silently_override_pinned_output_method():
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("不应发起请求"))
    ) as client:
        model = sdk_model(client)
        model.model_name = "gpt-4"
        profile = model_policy().profile.model_copy(update={"model_name": "gpt-4"})
        gate = RecordingGate()
        caller = BudgetedModelCaller(model, model_policy(profile=profile), gate)
        with pytest.raises(CallBudgetExceeded) as caught:
            await caller.invoke(
                Answer, [SystemMessage(content="返回 JSON")], CallOperation.SYNTHESIZE
            )
        assert caught.value.code == "model_budget_profile_mismatch"
        assert not hasattr(gate, "operation")
