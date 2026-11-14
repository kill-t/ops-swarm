"""调查 Worker 模型用量窗口测试。"""

from decimal import Decimal
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from opsswarm.investigation.attempts import ModelPricing
from opsswarm.worker.usage import ModelUsageTracker


def usage_result(*, input_tokens: int, output_tokens: int) -> LLMResult:
    return LLMResult(
        generations=[
            [
                ChatGeneration(
                    message=AIMessage(
                        content="完成",
                        response_metadata={
                            "token_usage": {
                                "prompt_tokens": input_tokens,
                                "completion_tokens": output_tokens,
                                "total_tokens": input_tokens + output_tokens,
                            }
                        },
                        usage_metadata={
                            "input_tokens": input_tokens,
                            "output_tokens": output_tokens,
                            "total_tokens": input_tokens + output_tokens,
                        },
                    )
                )
            ]
        ]
    )


def test_usage_tracker_counts_parallel_requests_tokens_and_cost() -> None:
    tracker = ModelUsageTracker(
        ModelPricing(
            input_cost_per_million_usd=Decimal("1"),
            output_cost_per_million_usd=Decimal("2"),
        )
    )
    first_id = uuid4()
    second_id = uuid4()
    tracker.begin_attempt()
    tracker.on_chat_model_start({}, [[HumanMessage(content="一")]], run_id=first_id)
    tracker.on_chat_model_start({}, [[HumanMessage(content="二")]], run_id=second_id)
    tracker.on_llm_end(usage_result(input_tokens=1000, output_tokens=500), run_id=first_id)
    tracker.on_llm_end(usage_result(input_tokens=2000, output_tokens=1000), run_id=second_id)

    telemetry = tracker.finish_attempt()

    assert telemetry.model_request_count == 2
    assert telemetry.usage_complete is True
    assert telemetry.input_tokens == 3000
    assert telemetry.output_tokens == 1500
    assert telemetry.total_tokens == 4500
    assert telemetry.estimated_cost_usd == Decimal("0.006000")


def test_usage_tracker_discards_callback_after_attempt_finished() -> None:
    tracker = ModelUsageTracker(None)
    stale_id = uuid4()
    tracker.begin_attempt()
    tracker.on_chat_model_start({}, [[HumanMessage(content="超时请求")]], run_id=stale_id)

    timed_out = tracker.finish_attempt()
    tracker.on_llm_end(usage_result(input_tokens=10, output_tokens=5), run_id=stale_id)
    tracker.begin_attempt()
    next_attempt = tracker.finish_attempt()

    assert timed_out.model_request_count == 1
    assert timed_out.usage_complete is False
    assert timed_out.estimated_cost_usd is None
    assert next_attempt.model_request_count == 0
    assert next_attempt.usage_complete is True
    assert next_attempt.total_tokens == 0


def test_usage_tracker_keeps_partial_tokens_but_does_not_estimate_total_cost() -> None:
    tracker = ModelUsageTracker(
        ModelPricing(
            input_cost_per_million_usd=Decimal("1"),
            output_cost_per_million_usd=Decimal("2"),
        )
    )
    completed_id = uuid4()
    failed_id = uuid4()
    tracker.begin_attempt()
    tracker.on_chat_model_start({}, [[HumanMessage(content="成功")]], run_id=completed_id)
    tracker.on_chat_model_start({}, [[HumanMessage(content="失败")]], run_id=failed_id)
    tracker.on_llm_end(
        usage_result(input_tokens=100, output_tokens=50),
        run_id=completed_id,
    )
    tracker.on_llm_error(TimeoutError(), run_id=failed_id)

    telemetry = tracker.finish_attempt()

    assert telemetry.model_request_count == 2
    assert telemetry.usage_complete is False
    assert telemetry.total_tokens == 150
    assert telemetry.estimated_cost_usd is None


def test_sdk_normalized_missing_usage_remains_unknown() -> None:
    """SDK 补零不能覆盖供应商未报告输出用量的事实。"""
    from langchain_openai import ChatOpenAI

    result = ChatOpenAI(api_key="offline-only", model="offline")._create_chat_result(
        {
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
            "usage": {"prompt_tokens": 10},
        }
    )
    tracker = ModelUsageTracker(
        ModelPricing(
            input_cost_per_million_usd=Decimal("1"),
            output_cost_per_million_usd=Decimal("2"),
        )
    )
    tracker.begin_attempt()
    request_id = uuid4()
    tracker.on_chat_model_start({}, [[HumanMessage(content="离线")]], run_id=request_id)
    tracker.on_llm_end(
        LLMResult(generations=[result.generations], llm_output=result.llm_output), run_id=request_id
    )
    usage = tracker.finish_attempt()
    assert usage.usage_complete is False
    assert usage.input_tokens == 10
    assert usage.estimated_cost_usd is None


@pytest.mark.parametrize(
    ("raw", "known", "complete"),
    [
        ({}, (0, 0), False),
        ({"prompt_tokens": 10}, (10, 0), False),
        ({"completion_tokens": 5}, (0, 5), False),
        ({"prompt_tokens": True, "completion_tokens": 5, "total_tokens": 6}, (0, 5), False),
        ({"prompt_tokens": -1, "completion_tokens": 5, "total_tokens": 4}, (0, 5), False),
        ({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 16}, (10, 5), False),
        ({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, (10, 5), True),
    ],
)
def test_usage_requires_consistent_raw_fields(raw, known, complete):
    tracker = ModelUsageTracker(None)
    tracker.begin_attempt()
    request_id = uuid4()
    tracker.on_chat_model_start({}, [[HumanMessage(content="用量边界")]], run_id=request_id)
    message = AIMessage(content="完成", response_metadata={"token_usage": raw})
    tracker.on_llm_end(
        LLMResult(generations=[[ChatGeneration(message=message)]]), run_id=request_id
    )
    usage = tracker.finish_attempt()
    assert (usage.input_tokens, usage.output_tokens) == known
    assert usage.usage_complete is complete


def test_multiple_candidates_do_not_double_count_request_usage():
    tracker = ModelUsageTracker(None)
    tracker.begin_attempt()
    request_id = uuid4()
    tracker.on_chat_model_start({}, [[HumanMessage(content="多候选")]], run_id=request_id)
    result = usage_result(input_tokens=10, output_tokens=5)
    result.generations[0].append(result.generations[0][0])
    tracker.on_llm_end(result, run_id=request_id)
    usage = tracker.finish_attempt()
    assert usage.total_tokens == 15
    assert usage.usage_complete is True
