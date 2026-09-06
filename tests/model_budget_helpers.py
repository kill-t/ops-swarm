"""模型预算测试的配置与离线 SDK 响应。"""

import json

import httpx2
from langchain_openai import ChatOpenAI

from opsswarm.investigation.budget.model_contracts import (
    ModelBudgetPolicy,
    ModelInput,
    ModelProfile,
    TextModelMessage,
)


def model_policy(**changes):
    return ModelBudgetPolicy(
        **{
            "profile": ModelProfile(
                profile_id="fake-v1",
                model_name="fake-budget",
                context_window_tokens=65536,
                structured_output_method="json_schema",
                pricing={"input_cost_per_million_usd": "1", "output_cost_per_million_usd": "2"},
            ),
            **changes,
        }
    )


def model_input(text="  保留原始文本  ", method="json_schema"):
    return ModelInput(
        messages=(TextModelMessage(role="system", content=text),),
        output_schema={"type": "object", "properties": {"answer": {"type": "integer"}}},
        tool_schema={},
        structured_output_method=method,
    )


def sdk_response(payload, result, *, usage=True, invalid_json=False):
    content = "not-json" if invalid_json else json.dumps(result, ensure_ascii=False)
    message = {"role": "assistant", "content": content}
    if "tools" in payload:
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "fake-tool",
                    "type": "function",
                    "function": {
                        "name": payload["tools"][0]["function"]["name"],
                        "arguments": content,
                    },
                }
            ],
        }
    body = {
        "id": "fake-completion",
        "object": "chat.completion",
        "created": 0,
        "model": "fake-budget",
        "choices": [{"index": 0, "finish_reason": "stop", "message": message}],
    }
    if usage:
        body["usage"] = {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}
    return httpx2.Response(200, json=body)


def sdk_model(client):
    return ChatOpenAI(
        api_key="test-only-key",
        model="fake-budget",
        base_url="https://model.invalid/v1",
        max_retries=0,
        n=1,
        http_async_client=client,
        use_responses_api=False,
    )
