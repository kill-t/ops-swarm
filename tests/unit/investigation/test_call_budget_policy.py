"""调用次数预算的边界与时限计算。"""

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallOperation
from opsswarm.investigation.budget.policy import (
    CallBudgetExceeded,
    operation_kind,
    operation_role,
    remaining_time,
    require_capacity,
)
from opsswarm.investigation.settings import LlmSettings, create_openai_compatible_model


@pytest.mark.parametrize(
    "field,value",
    [
        ("max_model_calls", 0),
        ("max_model_calls", 61),
        ("max_tool_calls", 101),
        ("max_tool_calls_per_task", 7),
        ("max_duration_seconds", 1801),
        ("max_model_calls", True),
        ("version", "new"),
    ],
)
def test_policy_rejects_invalid_or_unversioned_limits(field, value):
    with pytest.raises(ValidationError):
        CallBudgetPolicy(**{field: value})


@pytest.mark.parametrize(
    "operation,models,tools,task_tools,code",
    [
        (CallOperation.ANALYZE_LOG, 12, 0, 0, "model_call_budget_exceeded"),
        (CallOperation.SYNTHESIZE, 12, 0, 0, "model_call_budget_exceeded"),
        (CallOperation.REVIEW, 12, 0, 0, "model_call_budget_exceeded"),
        (CallOperation.HUMAN_REVIEW, 12, 0, 0, "model_call_budget_exceeded"),
        (CallOperation.QUERY_LOGS, 0, 18, 0, "tool_call_budget_exceeded"),
        (CallOperation.QUERY_METRICS, 0, 0, 6, "task_tool_budget_exceeded"),
    ],
)
def test_next_call_at_capacity_is_rejected(operation, models, tools, task_tools, code):
    with pytest.raises(CallBudgetExceeded) as caught:
        require_capacity(
            CallBudgetPolicy(), operation, models=models, tools=tools, task_tools=task_tools
        )
    assert caught.value.code == code


def test_deadline_and_operation_contract():
    started = datetime(2026, 9, 5, tzinfo=UTC)
    policy = CallBudgetPolicy(max_duration_seconds=10)
    assert remaining_time(policy, started, started + timedelta(seconds=9)) == 1
    with pytest.raises(CallBudgetExceeded) as caught:
        remaining_time(policy, started, started + timedelta(seconds=10))
    assert caught.value.code == "run_deadline_exceeded"
    for operation in CallOperation:
        require_capacity(policy, operation, models=0, tools=0, task_tools=0)
        assert operation_kind(operation) in ("model", "tool")
        assert (operation_role(operation) is None) == (
            operation
            in (
                CallOperation.COORDINATE,
                CallOperation.SYNTHESIZE,
                CallOperation.REVIEW,
                CallOperation.HUMAN_REVIEW,
                CallOperation.EVALUATE_CANDIDATE,
                CallOperation.COORDINATE_REINVESTIGATION,
                CallOperation.COLLECT_REINVESTIGATION,
                CallOperation.ANALYZE_REINVESTIGATION,
                CallOperation.SYNTHESIZE_REINVESTIGATION,
                CallOperation.REVIEW_REINVESTIGATION,
            )
        )


def test_budget_model_disables_sdk_retries_without_network():
    model = create_openai_compatible_model(
        LlmSettings(api_key="test-only-key", model="fake-model"),
        disable_retries=True,
    )
    assert model.max_retries == 0
    assert model.n == 1
    assert model.root_client.max_retries == 0
    assert model.root_async_client.max_retries == 0
