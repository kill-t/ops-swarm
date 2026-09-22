"""把独立审查编译为有限、可解释的确定性路由。"""

from opsswarm.investigation.coordination.contracts import CoordinationPolicy
from opsswarm.investigation.review.contracts import HypothesisReview
from opsswarm.investigation.review_routing.contracts import (
    ReviewRoute,
    ReviewRouteReason,
    ReviewRoutingDecision,
)
from opsswarm.investigation.synthesis.contracts import OpenHypothesis
from opsswarm.investigation.tasks.errors import TaskFactConflict


def compile_review_routing_decision(
    hypothesis: OpenHypothesis,
    review: HypothesisReview,
    *,
    policy: CoordinationPolicy,
    round_number: int,
    existing_task_count: int,
) -> ReviewRoutingDecision:
    """按证据异议和冻结任务容量决定下一路径，但不执行该路径。"""

    if type(round_number) is not int or not 1 <= round_number <= policy.max_rounds:
        raise TaskFactConflict("review_routing_round_invalid", "审查路由轮次不符合固定策略")
    if (
        type(existing_task_count) is not int
        or existing_task_count < 0
        or existing_task_count > policy.max_total_tasks
    ):
        raise TaskFactConflict(
            "review_routing_task_count_invalid", "审查路由的既有任务数不符合固定策略"
        )
    if not set(review.accepted_supporting_evidence_ids) <= set(hypothesis.supporting_evidence_ids):
        raise TaskFactConflict(
            "review_routing_source_invalid", "审查接受了开放假设未引用的支持证据"
        )

    reasons: list[ReviewRouteReason] = [
        "support_sufficient" if review.support_is_sufficient else "support_insufficient"
    ]
    if hypothesis.counter_evidence_ids:
        reasons.append("hypothesis_counter_evidence_present")
    if review.counter_evidence_ids:
        reasons.append("review_counter_evidence_present")
    if review.evidence_gaps:
        reasons.append("evidence_gaps_present")
    if review.competing_explanations:
        reasons.append("competing_explanations_present")

    suggestions = review.follow_up_suggestions
    remaining_rounds = policy.max_rounds - round_number
    remaining_task_slots = policy.max_total_tasks - existing_task_count
    route: ReviewRoute
    if review.support_is_sufficient:
        if len(reasons) == 1:
            route = "candidate_evaluation"
            reasons.append("candidate_requirements_satisfied")
        else:
            route = "await_human"
            reasons.append("follow_up_suggestions_missing")
    else:
        reasons.append(
            "follow_up_suggestions_present" if suggestions else "follow_up_suggestions_missing"
        )
        if suggestions and remaining_rounds < 1:
            reasons.append("round_limit_reached")
        if suggestions and len(suggestions) > policy.max_tasks_per_round:
            reasons.append("per_round_task_limit_exceeded")
        if suggestions and len(suggestions) > remaining_task_slots:
            reasons.append("total_task_limit_exceeded")
        if suggestions and not any(
            reason
            in {
                "round_limit_reached",
                "per_round_task_limit_exceeded",
                "total_task_limit_exceeded",
            }
            for reason in reasons
        ):
            route = "follow_up"
            reasons.append("follow_up_capacity_available")
        else:
            route = "await_human"

    return ReviewRoutingDecision(
        route=route,
        reason_codes=tuple(reasons),
        round_number=round_number,
        existing_task_count=existing_task_count,
        max_rounds=policy.max_rounds,
        max_tasks_per_round=policy.max_tasks_per_round,
        max_total_tasks=policy.max_total_tasks,
        remaining_rounds=remaining_rounds,
        remaining_task_slots=remaining_task_slots,
        suggested_task_count=len(suggestions),
        follow_up_suggestions=suggestions,
    )
