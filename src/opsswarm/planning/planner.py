"""从已通过质疑的根因与证据生成最小处置计划草案。"""

from collections.abc import Sequence

from opsswarm.investigation.models import (
    EvidenceObservation,
    HypothesisStatus,
    Recommendation,
    RootCause,
)
from opsswarm.planning.capabilities import assess_action_plan_capability
from opsswarm.planning.errors import ActionPlanCapabilityUnavailable, HypothesisNotSupported
from opsswarm.planning.models import ActionPlan, ActionPlanStatus
from opsswarm.planning.policy import classify_action_risk, validate_action_plan_policy
from opsswarm.planning.steps import build_candidate_step


def propose_action_plan(
    *,
    service_name: str,
    root_cause: RootCause,
    recommendation: Recommendation,
    hypothesis_status: HypothesisStatus,
    evidence_items: Sequence[EvidenceObservation],
    version: int = 1,
) -> ActionPlan:
    """只为通过质疑且具备安全参数的根因生成未授权草案。"""

    if hypothesis_status is not HypothesisStatus.SUPPORTED:
        raise HypothesisNotSupported(hypothesis_status)
    capability = assess_action_plan_capability(
        root_cause=root_cause,
        recommendation=recommendation,
        hypothesis_status=hypothesis_status,
    )
    if not capability.can_create:
        raise ActionPlanCapabilityUnavailable(capability)
    if capability.action_type is None:
        raise RuntimeError("可用计划能力没有对应的确定性模板实现")
    step, summary = build_candidate_step(service_name, capability.action_type, evidence_items)

    plan = ActionPlan(
        version=version,
        status=ActionPlanStatus.DRAFT,
        summary=summary,
        risk_level=step.risk_level,
        requires_human_approval=step.requires_human_approval,
        risk_policy_version=classify_action_risk(step.action_type, step.parameters).policy_version,
        steps=(step,),
    )
    validate_action_plan_policy(plan)
    return plan
