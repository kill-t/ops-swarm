"""完整文本输入估算、保守占用和模型预算的确定性判定。"""

import hashlib
import json
from collections.abc import Sequence
from decimal import ROUND_CEILING, Decimal

from opsswarm.investigation.budget.model_contracts import (
    ModelAllocation,
    ModelBudgetPolicy,
    ModelBudgetUsage,
    ModelProfile,
    ReportedModelUsage,
)
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.loop.contracts import AnyModelInput


def estimate_cost(profile: ModelProfile, inputs: int, outputs: int) -> Decimal:
    return (
        (
            inputs * profile.pricing.input_cost_per_million_usd
            + outputs * profile.pricing.output_cost_per_million_usd
        )
        / Decimal(1_000_000)
    ).quantize(Decimal("0.000001"), rounding=ROUND_CEILING)


def allocate_input(
    policy: ModelBudgetPolicy, model_input: AnyModelInput, *, prompt_version: str
) -> ModelAllocation:
    payload = json.dumps(
        model_input.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    if len(payload) > 262144:
        raise CallBudgetExceeded("model_input_too_large")
    estimated = len(payload) + 256
    if estimated > policy.max_input_tokens_per_call:
        raise CallBudgetExceeded("model_input_budget_exceeded")
    if (
        estimated + policy.max_output_tokens_per_call + policy.safety_margin_tokens
        > policy.profile.context_window_tokens
    ):
        raise CallBudgetExceeded("model_window_exceeded")
    if model_input.structured_output_method != policy.profile.structured_output_method:
        raise CallBudgetExceeded("model_budget_profile_mismatch")
    return ModelAllocation(
        prompt_version=prompt_version,
        input_sha256=hashlib.sha256(payload).hexdigest(),
        input_bytes=len(payload),
        estimated_input_tokens=estimated,
        output_token_limit=policy.max_output_tokens_per_call,
        reserved_cost_usd=estimate_cost(
            policy.profile, estimated, policy.max_output_tokens_per_call
        ),
    )


def summarize_model_usage(
    policy: ModelBudgetPolicy,
    records: Sequence[tuple[ModelAllocation, ReportedModelUsage | None]],
) -> ModelBudgetUsage:
    inputs = outputs = reported_inputs = reported_outputs = unknown = 0
    cost = reported_cost = Decimal(0)
    for allocation, usage in records:
        actual_cost = (
            estimate_cost(policy.profile, usage.input_tokens, usage.output_tokens)
            if usage
            else Decimal(0)
        )
        inputs += max(allocation.estimated_input_tokens, usage.input_tokens if usage else 0)
        outputs += max(allocation.output_token_limit, usage.output_tokens if usage else 0)
        cost += max(allocation.reserved_cost_usd, actual_cost)
        if usage is None:
            unknown += 1
        else:
            reported_inputs += usage.input_tokens
            reported_outputs += usage.output_tokens
            reported_cost += actual_cost
    return ModelBudgetUsage(
        committed_input_tokens=inputs,
        committed_output_tokens=outputs,
        committed_cost_usd=cost,
        reported_input_tokens=reported_inputs,
        reported_output_tokens=reported_outputs,
        reported_cost_usd=reported_cost if unknown == 0 else None,
        unknown_model_calls=unknown,
    )


def require_model_capacity(
    policy: ModelBudgetPolicy, usage: ModelBudgetUsage, allocation: ModelAllocation | None = None
) -> None:
    if (
        usage.committed_input_tokens + (allocation.estimated_input_tokens if allocation else 0)
        > policy.max_input_tokens
    ):
        raise CallBudgetExceeded("run_input_budget_exceeded")
    if (
        usage.committed_output_tokens + (allocation.output_token_limit if allocation else 0)
        > policy.max_output_tokens
    ):
        raise CallBudgetExceeded("run_output_budget_exceeded")
    if (
        usage.committed_cost_usd + (allocation.reserved_cost_usd if allocation else 0)
        > policy.max_estimated_cost_usd
    ):
        raise CallBudgetExceeded("model_cost_budget_exceeded")
