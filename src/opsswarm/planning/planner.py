"""从已通过质疑的根因与证据生成最小处置计划草案。"""

import re
from collections.abc import Sequence

from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    HypothesisStatus,
    RootCause,
)
from opsswarm.planning.errors import (
    HypothesisNotSupported,
    UnsafeRollbackTarget,
    UnsupportedPlanningRootCause,
)
from opsswarm.planning.models import (
    ActionPlan,
    ActionPlanStatus,
    ActionStep,
    ActionType,
    RollbackReleaseParameters,
    TargetType,
    VerificationOperator,
    VerificationRule,
)
from opsswarm.planning.policy import classify_action_risk, validate_action_plan_policy


def propose_action_plan(
    *,
    service_name: str,
    root_cause: RootCause,
    hypothesis_status: HypothesisStatus,
    evidence_items: Sequence[EvidenceObservation],
    version: int = 1,
) -> ActionPlan:
    """只为通过质疑且具备安全参数的根因生成未授权草案。"""

    if hypothesis_status is not HypothesisStatus.SUPPORTED:
        raise HypothesisNotSupported(hypothesis_status)
    if root_cause is not RootCause.RELEASE_500_REGRESSION:
        raise UnsupportedPlanningRootCause(root_cause)

    change = _latest_safe_deployment(evidence_items)
    parameters = RollbackReleaseParameters(
        current_version=str(change.attributes["to_version"]),
        target_version=str(change.attributes["from_version"]),
    )
    risk = classify_action_risk(ActionType.ROLLBACK_RELEASE, parameters)
    step = ActionStep(
        step_order=1,
        action_type=ActionType.ROLLBACK_RELEASE,
        target_type=TargetType.SERVICE,
        target_name=service_name,
        parameters=parameters,
        risk_level=risk.risk_level,
        requires_human_approval=risk.requires_human_approval,
        risk_rule_code=risk.rule_code,
        risk_rationale=risk.rationale,
        verification=VerificationRule(
            signal="http_5xx_ratio",
            operator=VerificationOperator.LESS_THAN,
            threshold=0.01,
            observation_window_seconds=60,
            required_successful_samples=3,
        ),
    )
    plan = ActionPlan(
        version=version,
        status=ActionPlanStatus.DRAFT,
        summary=(
            f"将 {service_name} 从 {parameters.current_version} 回滚到 "
            f"{parameters.target_version}，随后验证 5xx 比例持续低于 1%。"
        ),
        risk_level=risk.risk_level,
        requires_human_approval=risk.requires_human_approval,
        risk_policy_version=risk.policy_version,
        steps=(step,),
    )
    validate_action_plan_policy(plan)
    return plan


def _latest_safe_deployment(
    evidence_items: Sequence[EvidenceObservation],
) -> EvidenceObservation:
    candidates = [
        item
        for item in evidence_items
        if item.kind is EvidenceKind.CHANGE
        and item.attributes.get("change_type") == "deployment"
        and item.attributes.get("completed") is True
        and _valid_version(item.attributes.get("from_version"))
        and _valid_version(item.attributes.get("to_version"))
        and item.attributes["from_version"] != item.attributes["to_version"]
    ]
    if not candidates:
        raise UnsafeRollbackTarget()
    return max(candidates, key=lambda item: item.observed_at)


def _valid_version(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) <= 100
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", value) is not None
    )
