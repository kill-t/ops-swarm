"""复用确定性路由，继承完整原容量与预算；不授予动作资格。"""

from collections import Counter
from datetime import datetime

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.model_policy import estimate_cost
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_routing.contracts import UnresolvedConstraint
from opsswarm.investigation.human_wait.contracts import HumanWaitEventRecord
from opsswarm.investigation.reinvestigation.policy import budget_blockers
from opsswarm.investigation.reinvestigation_review.context import build_packet
from opsswarm.investigation.reinvestigation_review.contracts import (
    ReflowReviewResult,
    ReflowReviewSource,
)
from opsswarm.investigation.reinvestigation_review.policy import validate_proposal
from opsswarm.investigation.reinvestigation_routing.contracts import (
    BasisBlocker,
    Blocker,
    ReflowRoutingContent,
    RoutingWindow,
)
from opsswarm.investigation.review.contracts import HypothesisReview
from opsswarm.investigation.review_routing.policy import compile_review_routing_decision
from opsswarm.investigation.synthesis.contracts import OpenHypothesis


def compile_route(
    source: ReflowReviewSource,
    review: ReflowReviewResult,
    budget: CallBudgetSnapshot,
    *,
    now: datetime,
    basis_event_cursor: int,
    basis_blocker: BasisBlocker | None = None,
    manual_takeover: bool = False,
) -> ReflowRoutingContent:
    packet = build_packet(source)
    validate_proposal(source, review.proposal)
    # 完整重算用来校验账本，不沿用其中“再留一次协调调用”的特定准入假设。
    budget_blockers(budget, now=now)
    if (
        review.investigation_run_id != source.investigation_run_id
        or review.job_id != source.investigation_run_id
        or review.source_sha256 != content_hash(source)
        or review.synthesis_result_sha256 != source.synthesis_result_sha256
        or review.created_at < source.created_at
        or now < review.created_at
        or budget.investigation_run_id != source.investigation_run_id
        or budget.policy != source.call_budget
        or budget.model_budget != source.model_budget
        or budget.started_at != source.started_at
        or any(c.status == "reserved" for c in budget.calls)
    ):
        raise ValueError("路由必须绑定精确成功审查与原已结算账本")
    base = compile_review_routing_decision(
        OpenHypothesis.from_proposal(source.synthesis_result.proposal),
        HypothesisReview.from_proposal(review.proposal),
        policy=source.coordination_policy,
        round_number=source.next_round_number,
        existing_task_count=source.existing_task_count + len(source.professionals),
    )
    human = next(m for m in source.materials if m.kind == "human_inputs")
    events = tuple(HumanWaitEventRecord.model_validate(e) for e in human.content["events"])
    constraints = tuple(
        UnresolvedConstraint(event_id=e.id, item_index=i, text=item.text)
        for e in events
        if e.command.action == "supplement"
        for i, item in enumerate(e.command.items)
        if item.kind == "constraint"
    )
    models = (
        len(base.follow_up_suggestions) + 2
        if base.route == "follow_up"
        else int(base.route == "candidate_evaluation")
    )
    tools = len(base.follow_up_suggestions) if base.route == "follow_up" else 0
    blockers: list[Blocker] = [basis_blocker] if basis_blocker else []
    if manual_takeover:
        blockers.append("live_reflow_manual_takeover")
    if constraints:
        blockers.append("human_constraints_unresolved")
    assert budget.deadline is not None
    if now >= budget.deadline and "run_deadline_exceeded" not in blockers:
        blockers.append("run_deadline_exceeded")
    policy, model, usage = source.call_budget, source.model_budget, budget.model_usage
    assert (
        usage is not None
        and budget.reserved_model_calls is not None
        and budget.reserved_tool_calls is not None
    )
    if budget.reserved_model_calls + models > policy.max_model_calls:
        blockers.append("model_call_budget_exceeded")
    if budget.reserved_tool_calls + tools > policy.max_tool_calls or any(
        n > policy.max_tool_calls_per_task
        for n in Counter(c.scope_task_id for c in budget.calls if c.kind == "tool").values()
    ):
        blockers.append("tool_call_budget_exceeded")
    if (
        usage.committed_input_tokens + models * model.max_input_tokens_per_call
        > model.max_input_tokens
    ):
        blockers.append("run_input_budget_exceeded")
    if (
        usage.committed_output_tokens + models * model.max_output_tokens_per_call
        > model.max_output_tokens
    ):
        blockers.append("run_output_budget_exceeded")
    cost = estimate_cost(
        model.profile, model.max_input_tokens_per_call, model.max_output_tokens_per_call
    )
    if usage.committed_cost_usd + models * cost > model.max_estimated_cost_usd:
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
    return ReflowRoutingContent(
        investigation_run_id=source.investigation_run_id,
        review_source_sha256=content_hash(source),
        review_packet_sha256=content_hash(packet),
        review_result_sha256=content_hash(review),
        synthesis_result_sha256=source.synthesis_result_sha256,
        source_call_id=review.source_call_id,
        basis_event_cursor=basis_event_cursor,
        basis_blocker=basis_blocker,
        base_decision=base,
        route="await_human" if blockers else base.route,
        blocking_reason_codes=tuple(blockers),
        unresolved_constraints=constraints,
        unresolved_questions=source.synthesis_result.proposal.unresolved_questions,
        observation_windows=tuple(
            RoutingWindow(task_id=p.task_id, effective_window=p.effective_window)
            for p in source.professionals
        ),
        minimum_model_calls=models,
        minimum_tool_calls=tools,
        budget=budget,
        created_at=now,
    )
