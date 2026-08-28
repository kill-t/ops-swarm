"""真实模型评测预算、遥测与脱敏边界测试。"""

from decimal import Decimal
from uuid import uuid4

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from pydantic import ValidationError

from opsswarm.evaluation.attempts import (
    EvaluationAttemptBudget,
    EvaluationFailureCategory,
)
from opsswarm.evaluation.errors import EvaluationBudgetExceeded
from opsswarm.evaluation.real_model import (
    ModelUsageBudgetHandler,
    classify_evaluation_failure,
    real_model_experiment_spec,
    redact_failure_message,
)
from opsswarm.investigation.settings import LlmSettings


def _budget(**changes: object) -> EvaluationAttemptBudget:
    values: dict[str, object] = {
        "timeout_seconds": 60,
        "max_model_requests": 4,
        "max_tool_calls": 3,
        "max_output_tokens_per_request": 512,
        "max_estimated_cost_usd": Decimal("0.10"),
        "input_cost_per_million_usd": Decimal("1.00"),
        "output_cost_per_million_usd": Decimal("2.00"),
    }
    values.update(changes)
    return EvaluationAttemptBudget.model_validate(values)


def test_budget_rejects_output_ceiling_above_total_cost() -> None:
    with pytest.raises(ValidationError, match="理论费用已超过尝试预算"):
        _budget(
            max_model_requests=10,
            max_output_tokens_per_request=8192,
            output_cost_per_million_usd=Decimal("100"),
            max_estimated_cost_usd=Decimal("0.10"),
        )


def test_usage_handler_counts_requests_tokens_and_cost() -> None:
    handler = ModelUsageBudgetHandler(_budget())
    handler.on_chat_model_start(
        {},
        [[HumanMessage(content="测试")]],
        run_id=uuid4(),
    )
    handler.on_llm_end(
        LLMResult(
            generations=[
                [
                    ChatGeneration(
                        message=AIMessage(
                            content="完成",
                            usage_metadata={
                                "input_tokens": 1000,
                                "output_tokens": 500,
                                "total_tokens": 1500,
                            },
                        )
                    )
                ]
            ]
        ),
        run_id=uuid4(),
    )

    telemetry = handler.telemetry()
    assert telemetry.model_request_count == 1
    assert telemetry.total_tokens == 1500
    assert telemetry.estimated_cost_usd == Decimal("0.002000")


def test_usage_handler_rejects_next_request_before_network_call() -> None:
    handler = ModelUsageBudgetHandler(_budget(max_model_requests=1))
    handler.on_chat_model_start({}, [[HumanMessage(content="第一次")]], run_id=uuid4())

    with pytest.raises(EvaluationBudgetExceeded, match="请求次数"):
        handler.on_chat_model_start({}, [[HumanMessage(content="第二次")]], run_id=uuid4())


def test_model_parameter_hash_does_not_contain_api_key() -> None:
    settings = LlmSettings(api_key="secret-value", model="test-model")
    spec = real_model_experiment_spec(
        "real-model-exp01",
        llm_settings=settings,
        budget=_budget(),
    )

    serialized = spec.model_dump_json()
    assert "secret-value" not in serialized
    assert spec.model_parameters_hash is not None
    assert len(spec.model_parameters_hash) == 64


def test_failure_message_is_redacted_and_bounded() -> None:
    message = redact_failure_message(
        "供应商拒绝 secret-value " + "x" * 600,
        secrets=("secret-value",),
    )

    assert "secret-value" not in message
    assert "[已脱敏]" in message
    assert len(message) == 500


def test_timeout_has_stable_failure_category() -> None:
    assert classify_evaluation_failure(TimeoutError()) is EvaluationFailureCategory.TIMEOUT
