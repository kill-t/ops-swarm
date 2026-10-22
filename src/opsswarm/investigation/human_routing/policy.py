"""只编译后续意图；不分派任务、不把人工陈述升级为证据或授权。"""

import json
from datetime import datetime, timedelta

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.model_policy import estimate_cost
from opsswarm.investigation.coordination.contracts import CoordinationPolicy
from opsswarm.investigation.human_prepare.contracts import PreparationSource
from opsswarm.investigation.human_routing.contracts import (
    Blocker,
    HumanRoutingDecision,
    UnresolvedConstraint,
)
from opsswarm.investigation.review.contracts import HypothesisReview, ReviewModelResult
from opsswarm.investigation.review.policy import validate_review_evidence
from opsswarm.investigation.review_routing.policy import compile_review_routing_decision
from opsswarm.investigation.synthesis.contracts import OpenHypothesisRecord


def compile_human_routing(
    source: PreparationSource,
    result: ReviewModelResult,
    budget: CallBudgetSnapshot,
    *,
    now: datetime,
) -> HumanRoutingDecision:
    original = source.route.decision
    material = json.loads(next(m.content for m in source.prior_input.messages if m.role == "user"))
    hypothesis = OpenHypothesisRecord.model_validate(material["open_hypothesis"])
    validate_review_evidence(
        result.proposal,
        hypothesis.hypothesis,
        [item["evidence_id"] for item in material["evidence"]],
    )
    base = compile_review_routing_decision(
        hypothesis.hypothesis,
        HypothesisReview.from_proposal(result.proposal),
        policy=CoordinationPolicy(
            max_rounds=original.max_rounds,
            max_tasks_per_round=original.max_tasks_per_round,
            max_total_tasks=original.max_total_tasks,
        ),
        round_number=original.round_number,
        existing_task_count=original.existing_task_count,
    )
    constraints = tuple(
        UnresolvedConstraint(event_id=event.id, item_index=index, text=item.text)
        for event in source.wait.events
        if event.command.action == "supplement"
        for index, item in enumerate(event.command.items)
        if item.kind == "constraint"
    )
    if (
        budget.investigation_run_id != source.handoff.investigation_run_id
        or budget.policy != source.call_budget
        or budget.model_budget != source.model_budget
        or budget.started_at != source.started_at
        or budget.deadline
        != source.started_at + timedelta(seconds=source.call_budget.max_duration_seconds)
        or budget.reserved_model_calls is None
        or budget.reserved_tool_calls is None
        or budget.model_usage is None
        or any(c.status == "reserved" for c in budget.calls)
    ):
        raise ValueError("后续意图需要原预算和全部已结算账本")
    models = (
        len(base.follow_up_suggestions) + 2
        if base.route == "follow_up"
        else int(base.route == "candidate_evaluation")
    )
    tools = len(base.follow_up_suggestions) if base.route == "follow_up" else 0
    blockers: list[Blocker] = ["human_constraints_unresolved"] if constraints else []
    if now >= budget.deadline:
        blockers.append("run_deadline_exceeded")
    if models and budget.reserved_model_calls + models > source.call_budget.max_model_calls:
        blockers.append("model_call_budget_exceeded")
    if tools and budget.reserved_tool_calls + tools > source.call_budget.max_tool_calls:
        blockers.append("tool_call_budget_exceeded")
    policy, usage = source.model_budget, budget.model_usage
    if models and usage.committed_input_tokens + models > policy.max_input_tokens:
        blockers.append("run_input_budget_exceeded")
    if models and (
        usage.committed_output_tokens + models * policy.max_output_tokens_per_call
        > policy.max_output_tokens
    ):
        blockers.append("run_output_budget_exceeded")
    if models and (
        usage.committed_cost_usd
        + models * estimate_cost(policy.profile, 1, policy.max_output_tokens_per_call)
        > policy.max_estimated_cost_usd
    ):
        blockers.append("model_cost_budget_exceeded")
    if any(
        c.model_usage
        and c.model_allocation
        and (
            c.model_usage.input_tokens > c.model_allocation.estimated_input_tokens
            or c.model_usage.output_tokens > c.model_allocation.output_token_limit
        )
        for c in budget.calls
    ):
        blockers.append("model_usage_exceeded_allocation")
    return HumanRoutingDecision(
        base_decision=base,
        route="await_human" if blockers else base.route,
        blocking_reason_codes=tuple(blockers),
        unresolved_constraints=constraints,
        minimum_model_calls=models,
        minimum_tool_calls=tools,
        budget=budget,
    )
