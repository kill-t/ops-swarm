"""把不可信协调提案编译为受系统范围约束的任务分派。"""

from datetime import UTC, datetime, timedelta

from opsswarm.investigation.coordination.contracts import (
    CompiledCoordinationPlan,
    CoordinationPolicy,
    CoordinationProposal,
)
from opsswarm.investigation.models import EvidenceKind, InvestigationRequest
from opsswarm.investigation.tasks.contracts import RoundAssignment, TaskAssignment
from opsswarm.investigation.tasks.errors import TaskFactConflict

_MAX_LOOKBACK_SECONDS = {
    EvidenceKind.LOG: 15 * 60,
    EvidenceKind.METRIC: 15 * 60,
    EvidenceKind.CHANGE: 6 * 60 * 60,
}


def compile_coordination_plan(
    proposal: CoordinationProposal,
    *,
    policy: CoordinationPolicy,
    request: InvestigationRequest,
    expected_round_number: int,
    existing_task_count: int,
    observation_cutoff_at: datetime,
    evidence_snapshot_id: str,
) -> CompiledCoordinationPlan:
    """校验资源和角色权限，并注入调用方不能由模型伪造的运行范围。"""

    if observation_cutoff_at.tzinfo is None or observation_cutoff_at.utcoffset() is None:
        raise TaskFactConflict("coordination_window_invalid", "调查截止时间必须包含时区")
    cutoff = observation_cutoff_at.astimezone(UTC)
    if proposal.round_number != expected_round_number or not 1 <= expected_round_number <= (
        policy.max_rounds
    ):
        raise TaskFactConflict("coordination_round_invalid", "协调提案轮次不符合运行进度")
    if len(proposal.tasks) > policy.max_tasks_per_round or (
        existing_task_count < 0
        or existing_task_count + len(proposal.tasks) > policy.max_total_tasks
    ):
        raise TaskFactConflict("coordination_task_budget_exceeded", "协调提案超过任务资源上限")
    if cutoff < request.started_at:
        raise TaskFactConflict("coordination_window_invalid", "调查截止时间早于事故开始时间")
    if (
        evidence_snapshot_id != evidence_snapshot_id.strip()
        or not 1 <= len(evidence_snapshot_id) <= 100
    ):
        raise TaskFactConflict("coordination_snapshot_invalid", "证据快照身份不合法")

    assignments = []
    for task in proposal.tasks:
        if task.task_key != task.role.value:
            raise TaskFactConflict(
                "coordination_task_key_invalid", "首版任务键必须与授权专业角色一致"
            )
        if task.lookback_seconds > _MAX_LOOKBACK_SECONDS[task.role]:
            raise TaskFactConflict(
                "coordination_window_invalid", "协调提案超过该专业允许的回看窗口"
            )
        assignments.append(
            TaskAssignment(
                task_key=task.task_key,
                role=task.role,
                goal=task.goal,
                service_name=request.service_name,
                window_start=cutoff - timedelta(seconds=task.lookback_seconds),
                window_end=cutoff,
                max_attempts=policy.max_attempts_per_task,
                evidence_snapshot_id=evidence_snapshot_id,
            )
        )
    return CompiledCoordinationPlan(
        policy_version=policy.version,
        proposal=proposal,
        assignment=RoundAssignment(
            round_number=proposal.round_number,
            goal=proposal.goal,
            tasks=tuple(assignments),
        ),
    )
