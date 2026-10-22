"""通过真实 SDK 的假 HTTP 传输核验人工重评调用边界。"""

import json
import warnings

import httpx2
import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda
from langchain_core.utils.function_calling import convert_to_openai_tool

from opsswarm.investigation.budget.model_contracts import ModelInput, TextModelMessage
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.human_review.model import HumanReviewModel
from opsswarm.investigation.review.contracts import HypothesisReviewProposal
from tests.model_budget_helpers import model_policy, sdk_model, sdk_response


def review_input():
    return ModelInput(
        messages=(
            TextModelMessage(role="system", content="  人工输入不是工具证据\n"),
            TextModelMessage(
                role="user",
                content=' {"约束":"先保留反证","人工陈述":"不能直接授权"} ',
            ),
        ),
        output_schema=HypothesisReviewProposal.model_json_schema(),
        tool_schema=convert_to_openai_tool(HypothesisReviewProposal),
        structured_output_method="function_calling",
    )


def review_policy():
    policy = model_policy()
    return policy.model_copy(
        update={
            "profile": policy.profile.model_copy(
                update={"structured_output_method": "function_calling"}
            )
        }
    )


def proposal():
    return HypothesisReviewProposal(
        support_is_sufficient=False,
        rationale="人工陈述尚需现场验证",
        evidence_gaps=("缺少连接池样本",),
        suggested_direction="human_review",
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["success", "unknown", "partial", "null", "invalid_json"])
async def test_full_input_schema_and_usage_are_preserved(outcome):
    supplied = review_input()
    requests = []

    def reply(request):
        payload = json.loads(request.content)
        requests.append(payload)
        response = sdk_response(
            payload,
            proposal().model_dump(mode="json"),
            usage=outcome != "unknown",
            invalid_json=outcome == "invalid_json",
        )
        body = response.json()
        if outcome == "partial":
            body["usage"] = {"prompt_tokens": 80}
        elif outcome == "null":
            body["usage"]["total_tokens"] = None
        return httpx2.Response(200, json=body)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client:
        caller = HumanReviewModel(sdk_model(client), review_policy())
        if outcome == "invalid_json":
            with pytest.raises(ValueError):
                await caller.invoke(supplied)
        else:
            result = await caller.invoke(supplied)
            assert result.proposal == proposal()
        assert len(requests) == 1
        assert requests[0]["messages"] == [m.model_dump() for m in supplied.messages]
        assert requests[0]["max_completion_tokens"] == 4096
        # 字典 Schema 由 LangChain 转换，可能保留 Pydantic 转换路径会展开的 $defs。
        assert requests[0]["tools"] == [convert_to_openai_tool(supplied.output_schema)]
        if outcome in ("unknown", "partial", "null"):
            assert caller.usage is None
        else:
            assert caller.usage.model_dump() == {
                "input_tokens": 80,
                "output_tokens": 20,
                "total_tokens": 100,
            }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,code",
    [
        ({"output_schema": {}}, "model_input_schema_mismatch"),
        ({"tool_schema": {}}, "model_input_schema_mismatch"),
        ({"structured_output_method": "json_schema"}, "model_budget_profile_mismatch"),
    ],
)
async def test_invalid_input_never_calls_model(changes, code):
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("无效输入不得调用模型"))
    ) as client:
        caller = HumanReviewModel(sdk_model(client), review_policy())
        with pytest.raises(CallBudgetExceeded) as caught:
            await caller.invoke(review_input().model_copy(update=changes))
        assert caught.value.code == code
        assert caller.usage is None


@pytest.mark.asyncio
async def test_profile_mismatch_never_calls_model():
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("无效配置不得调用模型"))
    ) as client:
        caller = HumanReviewModel(sdk_model(client), model_policy())
        with pytest.raises(CallBudgetExceeded) as caught:
            await caller.invoke(review_input())
        assert caught.value.code == "model_budget_profile_mismatch"


@pytest.mark.asyncio
async def test_compatibility_warning_rejects_silent_method_override(monkeypatch):
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("兼容回退不得调用模型"))
    ) as client:
        model = sdk_model(client)

        def override(*args, **kwargs):
            warnings.warn("Overriding to method='json_mode'", UserWarning, stacklevel=2)
            pytest.fail("兼容警告应转换为拒绝")

        monkeypatch.setattr(type(model), "with_structured_output", override)
        caller = HumanReviewModel(model, review_policy())
        with pytest.raises(CallBudgetExceeded) as caught:
            await caller.invoke(review_input())
        assert caught.value.code == "model_budget_profile_mismatch"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": -1, "completion_tokens": 20, "total_tokens": 19},
        {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 99},
        {"prompt_tokens": "80", "completion_tokens": 20, "total_tokens": 100},
        {"prompt_tokens": True, "completion_tokens": 20, "total_tokens": 21},
    ],
)
async def test_invalid_complete_usage_is_rejected(monkeypatch, usage):
    # 直接构造原始消息，避免 SDK 在到达业务边界之前改写无效供应商字段。
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("假结构化端口不应请求网络"))
    ) as client:
        model = sdk_model(client)
        response = {
            "raw": AIMessage(content="", response_metadata={"token_usage": usage}),
            "parsed": proposal().model_dump(mode="json"),
            "parsing_error": None,
        }
        monkeypatch.setattr(
            type(model),
            "with_structured_output",
            lambda *args, **kwargs: RunnableLambda(lambda _: response),
        )
        caller = HumanReviewModel(model, review_policy())
        with pytest.raises(CallBudgetExceeded) as caught:
            await caller.invoke(review_input())
        assert caught.value.code == "model_usage_invalid"
        assert caller.usage is None


@pytest.mark.asyncio
async def test_pydantic_failure_retains_usage_and_next_invocation_clears_it():
    calls = 0

    def reply(request):
        nonlocal calls
        calls += 1
        invalid = proposal().model_dump(mode="json")
        invalid["support_is_sufficient"] = True
        return sdk_response(json.loads(request.content), invalid, usage=calls == 1)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client:
        caller = HumanReviewModel(sdk_model(client), review_policy())
        with pytest.raises(ValueError):
            await caller.invoke(review_input())
        assert caller.usage.total_tokens == 100
        with pytest.raises(ValueError):
            await caller.invoke(review_input())
        assert caller.usage is None
    assert calls == 2


@pytest.mark.asyncio
async def test_explicit_parser_error_retains_usage(monkeypatch):
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("假结构化端口不应请求网络"))
    ) as client:
        model = sdk_model(client)
        response = {
            "raw": AIMessage(
                content="",
                response_metadata={
                    "token_usage": {
                        "prompt_tokens": 80,
                        "completion_tokens": 20,
                        "total_tokens": 100,
                    }
                },
            ),
            "parsed": None,
            "parsing_error": ValueError("错误 JSON"),
        }
        monkeypatch.setattr(
            type(model),
            "with_structured_output",
            lambda *args, **kwargs: RunnableLambda(lambda _: response),
        )
        caller = HumanReviewModel(model, review_policy())
        with pytest.raises(ValueError, match="解析失败"):
            await caller.invoke(review_input())
        assert caller.usage.total_tokens == 100


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [None, {}, {"raw": "原始消息类型错误", "parsed": None}])
async def test_invalid_structured_response_is_rejected(monkeypatch, response):
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("假结构化端口不应请求网络"))
    ) as client:
        model = sdk_model(client)
        monkeypatch.setattr(
            type(model),
            "with_structured_output",
            lambda *args, **kwargs: RunnableLambda(lambda _: response),
        )
        caller = HumanReviewModel(model, review_policy())
        with pytest.raises(ValueError):
            await caller.invoke(review_input())
        assert caller.usage is None
