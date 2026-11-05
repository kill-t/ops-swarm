"""回流决定的精确内容、动作时限及独立阶段投影。"""

from datetime import datetime

from opsswarm.approval.decision.contracts import DecisionError
from opsswarm.approval.decision.policy import target as target
from opsswarm.approval.decision.policy import validate_timing as candidate_timing
from opsswarm.approval.reinvestigation_candidate.contracts import (
    ReflowRequestRecord,
    RequestAvailability,
)
from opsswarm.approval.reinvestigation_decision.contracts import (
    ReflowDecisionAction,
    ReflowDecisionAvailability,
    ReflowDecisionCommand,
    ReflowDecisionContent,
    ReflowDecisionError,
    ReflowDecisionRecord,
)
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.planning.reinvestigation_candidate.contracts import ReflowPlanRecord


def build_content(
    request: ReflowRequestRecord, plan: ReflowPlanRecord, *, action: ReflowDecisionAction
) -> ReflowDecisionContent:
    if (
        request.plan_id != plan.id
        or request.investigation_run_id != plan.investigation_run_id
        or request.candidate_job_id != plan.candidate_job_id
        or request.plan_content_sha256 != plan.content_sha256
        or plan.content_sha256 != content_hash(plan.content)
        or request.previous_incident_version != plan.incident_version
        or plan.content.candidate_job_id != plan.candidate_job_id
    ):
        raise ReflowDecisionError("reflow_decision_source_invalid", "请求与完整原草案不一致")
    return ReflowDecisionContent(
        request_id=request.id,
        plan_id=plan.id,
        plan_content_sha256=plan.content_sha256,
        outcome=target(action)[0],
        plan=plan.content,
    )


def validate_timing(action: ReflowDecisionAction, *, expires_at: datetime, now: datetime) -> None:
    try:
        candidate_timing(action, expires_at=expires_at, now=now)
    except DecisionError as error:
        raise ReflowDecisionError(
            error.code.replace("candidate_decision_", "reflow_decision_"), str(error)
        ) from error


def validate_transition(
    command: ReflowDecisionCommand,
    request: ReflowRequestRecord,
    *,
    availability: RequestAvailability,
    incident_status: str | None,
    incident_version: int | None,
    now: datetime,
) -> None:
    if command.request_id != request.id:
        raise ReflowDecisionError("reflow_decision_request_mismatch", "命令不匹配审批请求")
    if command.plan_content_sha256 != request.plan_content_sha256:
        raise ReflowDecisionError("reflow_decision_plan_mismatch", "命令不匹配完整原草案内容")
    if (
        incident_status != "awaiting_approval"
        or incident_version != request.incident_version
        or command.expected_incident_version != request.incident_version
    ):
        raise ReflowDecisionError(
            "reflow_decision_incident_version_conflict", "事故阶段或版本已变化"
        )
    validate_timing(command.action, expires_at=request.expires_at, now=now)
    if command.action == "approve" and availability != "current":
        raise ReflowDecisionError("reflow_decision_source_stale", "原审批依据已失效，不能批准")
    if command.action == "reject" and availability not in {"current", "stale"}:
        raise ReflowDecisionError("reflow_decision_source_stale", "原请求不处于可拒绝阶段")


def decision_availability(
    record: ReflowDecisionRecord,
    *,
    basis_current: bool,
    expires_at: datetime,
    now: datetime,
) -> ReflowDecisionAvailability:
    if any(value.tzinfo is None or value.utcoffset() is None for value in (now, expires_at)):
        raise ValueError("决定阶段时间必须包含时区")
    if record.outcome != "approved":
        return "recorded"
    if now >= expires_at:
        return "expired"
    return "current" if basis_current else "stale"
