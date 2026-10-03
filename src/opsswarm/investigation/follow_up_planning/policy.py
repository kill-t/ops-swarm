"""把已验证补查路由完整编译为下一轮有限协调提案。"""

from opsswarm.investigation.coordination.contracts import (
    CoordinationProposal,
    ProposedTask,
)
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord
from opsswarm.investigation.tasks.errors import TaskFactConflict

FOLLOW_UP_ROUND_GOAL = "补查独立审查指出的证据缺口"

_LOOKBACK_SECONDS = {
    EvidenceKind.LOG: 15 * 60,
    EvidenceKind.METRIC: 15 * 60,
    EvidenceKind.CHANGE: 6 * 60 * 60,
}


def compile_follow_up_proposal(route: ReviewRoutingRecord) -> CoordinationProposal:
    """保留全部补查意图并以系统规则补齐任务键、轮次和窗口。"""

    decision = route.decision
    if decision.route != "follow_up":
        raise TaskFactConflict("follow_up_route_required", "只有补查路由可以编译下一轮计划")
    if (
        decision.round_number >= decision.max_rounds
        or not decision.follow_up_suggestions
        or len(decision.follow_up_suggestions) > decision.max_tasks_per_round
        or len(decision.follow_up_suggestions) > decision.remaining_task_slots
    ):
        raise TaskFactConflict("follow_up_capacity_invalid", "补查路由不再满足固定资源上限")
    suggestions = tuple(sorted(decision.follow_up_suggestions, key=lambda item: item.role.value))
    return CoordinationProposal(
        round_number=decision.round_number + 1,
        goal=FOLLOW_UP_ROUND_GOAL,
        tasks=tuple(
            ProposedTask(
                task_key=item.role.value,
                role=item.role,
                goal=item.question,
                lookback_seconds=_LOOKBACK_SECONDS[item.role],
            )
            for item in suggestions
        ),
    )
