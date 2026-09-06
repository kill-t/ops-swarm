"""模型预算不依赖供应商服务的输入估算与占用规则。"""

from decimal import Decimal

import pytest
from pydantic import ValidationError

from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.budget.model_policy import (
    allocate_input,
    require_model_capacity,
    summarize_model_usage,
)
from opsswarm.investigation.budget.model_settings import ModelBudgetSettings
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.settings import LlmSettings
from tests.model_budget_helpers import model_input, model_policy


@pytest.mark.parametrize(
    "changed",
    [
        {"max_input_tokens_per_call": True},
        {"max_output_tokens_per_call": 0},
        {"safety_margin_tokens": 127},
        {"max_estimated_cost_usd": "101"},
        {"max_input_tokens_per_call": 65000},
        {"version": "unknown"},
    ],
)
def test_model_budget_rejects_invalid_limits(changed):
    with pytest.raises(ValidationError):
        model_policy(**changed)


def test_full_input_estimate_preserves_whitespace_and_tracks_schema_changes():
    policy = model_policy()
    original = model_input()
    assert original.messages[0].content == "  保留原始文本  "
    allocation = allocate_input(policy, original, prompt_version="test-v1")
    changed = original.model_copy(update={"output_schema": {"description": "schema 内容变长" * 20}})
    other = allocate_input(policy, changed, prompt_version="test-v1")
    assert other.input_sha256 != allocation.input_sha256
    assert other.estimated_input_tokens > allocation.estimated_input_tokens
    assert allocation.reserved_cost_usd > 0
    with pytest.raises(CallBudgetExceeded, match="预算"):
        allocate_input(policy, model_input("长日志" * 10000), prompt_version="test-v1")


@pytest.mark.parametrize("kind", ["input", "output", "cost"])
def test_exact_capacity_and_unknown_usage_keep_entire_reservation(kind):
    policy = model_policy()
    allocation = allocate_input(policy, model_input(), prompt_version="test-v1")
    changes = {
        "input": {"max_input_tokens": allocation.estimated_input_tokens},
        "output": {"max_output_tokens": allocation.output_token_limit},
        "cost": {"max_estimated_cost_usd": allocation.reserved_cost_usd},
    }[kind]
    policy = model_policy(**changes)
    empty = summarize_model_usage(policy, [])
    require_model_capacity(policy, empty, allocation)
    charged = summarize_model_usage(policy, [(allocation, None)])
    assert charged.unknown_model_calls == 1 and charged.reported_cost_usd is None
    with pytest.raises(CallBudgetExceeded):
        require_model_capacity(policy, charged, allocation)


def test_reported_overrun_is_preserved_and_small_usage_does_not_refund():
    policy = model_policy()
    allocation = allocate_input(policy, model_input(), prompt_version="test-v1")
    low = ReportedModelUsage(input_tokens=1, output_tokens=1, total_tokens=2)
    summary = summarize_model_usage(policy, [(allocation, low)])
    assert summary.committed_cost_usd == allocation.reserved_cost_usd
    assert summary.reported_cost_usd == Decimal("0.000003")
    high = ReportedModelUsage(input_tokens=1000000, output_tokens=1000000, total_tokens=2000000)
    summary = summarize_model_usage(policy, [(allocation, high)])
    assert summary.committed_cost_usd == Decimal("3")
    with pytest.raises(CallBudgetExceeded):
        require_model_capacity(policy, summary)
    with pytest.raises(ValidationError):
        ReportedModelUsage(input_tokens=1, output_tokens=2, total_tokens=2)


def test_profile_requires_complete_explicit_settings():
    profile = model_policy().profile
    llm = LlmSettings(api_key="fake", model="fake-budget")
    assert (
        ModelBudgetSettings(budget_profile_id="", context_window_tokens="").to_profile(llm, None)
        is None
    )
    settings = ModelBudgetSettings(budget_profile_id="fake-v1", context_window_tokens=65536)
    assert settings.to_profile(llm, None) is None
    assert settings.to_profile(llm, profile.pricing) == profile
