"""候选审批决定的内容、状态迁移与历史适用性规则。"""

from datetime import UTC, datetime

from opsswarm.approval.candidate.contracts import RequestAvailability, RequestRecord
from opsswarm.approval.decision.contracts import (
    DecisionAction,
    DecisionAvailability,
    DecisionCommand,
    DecisionContent,
    DecisionError,
    DecisionOutcome,
    DecisionRecord,
)
from opsswarm.incidents.models import IncidentAction
from opsswarm.investigation.candidate.contracts import SourceKind
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.planning.candidate.contracts import PlanRecord


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("审批决定时间必须包含时区")
    return value.astimezone(UTC)


def target(action: DecisionAction) -> tuple[DecisionOutcome, IncidentAction, str]:
    if action == "approve":
        return "approved", IncidentAction.APPROVE_PLAN, "mitigating"
    if action == "reject":
        return "rejected", IncidentAction.REJECT_PLAN, "investigating"
    if action == "expire":
        return "expired", IncidentAction.EXPIRE_APPROVAL, "investigating"
    raise ValueError("不支持的候选审批动作")


def build_content(
    request: RequestRecord,
    plan: PlanRecord,
    *,
    source_content_sha256: str,
    action: DecisionAction,
) -> DecisionContent:
    """只复制精确绑定的原草案，确认不会抹去未决条件。"""

    if (
        request.plan_id != plan.id
        or request.investigation_run_id != plan.investigation_run_id
        or request.candidate_job_id != plan.candidate_job_id
        or request.plan_content_sha256 != plan.content_sha256
        or plan.content_sha256 != content_hash(plan.content)
        or request.wait_version != plan.wait_version
        or request.previous_incident_version != plan.incident_version
        or plan.content.candidate_job_id != plan.candidate_job_id
    ):
        raise DecisionError("candidate_decision_source_invalid", "审批请求与原草案内容不一致")
    outcome, _, _ = target(action)
    return DecisionContent(
        request_id=request.id,
        plan_id=plan.id,
        plan_content_sha256=plan.content_sha256,
        candidate_job_id=plan.candidate_job_id,
        source_content_sha256=source_content_sha256,
        wait_version=request.wait_version,
        outcome=outcome,
        evidence_ids=plan.content.evidence_ids,
        preconditions=plan.content.preconditions,
        unresolved_questions=plan.content.unresolved_questions,
    )


def validate_transition(
    command: DecisionCommand,
    request: RequestRecord,
    *,
    availability: RequestAvailability,
    incident_status: str | None,
    incident_version: int | None,
    now: datetime,
) -> None:
    """所有动作绑定同一事故阶段，负面决定只放宽来源和剩余额度。"""

    if command.request_id != request.id:
        raise DecisionError("candidate_decision_request_mismatch", "命令不匹配审批请求")
    if command.plan_content_sha256 != request.plan_content_sha256:
        raise DecisionError("candidate_decision_plan_mismatch", "命令不匹配完整原草案内容")
    if (
        incident_status != "awaiting_approval"
        or incident_version != request.incident_version
        or command.expected_incident_version != request.incident_version
    ):
        raise DecisionError("candidate_decision_incident_version_conflict", "事故阶段或版本已变化")
    validate_timing(command.action, expires_at=request.expires_at, now=now)
    if command.action == "expire":
        return
    if command.action == "approve" and availability != "current":
        raise DecisionError("candidate_decision_source_stale", "原审批输入已失效，不能批准")
    if command.action == "reject" and availability not in {"current", "stale"}:
        raise DecisionError("candidate_decision_source_stale", "原审批请求不处于可拒绝阶段")


def validate_timing(action: DecisionAction, *, expires_at: datetime, now: datetime) -> None:
    """写后仅复查动作期限，不因本次合法事故迁移拒绝自己的决定。"""

    current_time, deadline = _utc(now), _utc(expires_at)
    if action == "expire":
        if current_time < deadline:
            raise DecisionError("candidate_decision_not_expired", "审批请求尚未到期")
    elif current_time >= deadline:
        raise DecisionError("candidate_decision_expired", "审批请求已到期，必须显式提交过期命令")


def decision_availability(
    record: DecisionRecord,
    *,
    source_kind: SourceKind,
    run_status: str,
    latest_wait_version: int | None,
    wait_cancelled: bool,
    incident_status: str | None,
    incident_version: int | None,
    expires_at: datetime,
    now: datetime,
) -> DecisionAvailability:
    """批准以后按新阶段投影；负面结果是历史事实，不提供执行资格。"""

    if record.outcome != "approved":
        return "recorded"
    if _utc(now) >= _utc(expires_at):
        return "expired"
    source_current = (
        source_kind == "terminal_handoff"
        and run_status == "await_candidate"
        and record.content.wait_version is None
    ) or (
        source_kind == "human_route"
        and run_status == "await_human"
        and not wait_cancelled
        and record.content.wait_version is not None
        and latest_wait_version == record.content.wait_version
    )
    if (
        not source_current
        or incident_status != "mitigating"
        or incident_version != record.incident_version
    ):
        return "stale"
    return "current"
