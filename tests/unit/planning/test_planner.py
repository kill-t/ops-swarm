"""版本化处置计划生成规则的纯单元测试。"""

import pytest

from opsswarm.investigation.models import (
    EvidenceKind,
    HypothesisStatus,
    Recommendation,
    RootCause,
)
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from opsswarm.planning import (
    ActionPlanCapabilityUnavailable,
    ActionPlanStatus,
    ActionType,
    HypothesisNotSupported,
    RiskLevel,
    UnsafeRollbackTarget,
    propose_action_plan,
)


def test_supported_release_hypothesis_generates_high_risk_rollback_draft() -> None:
    plan = propose_action_plan(
        service_name="order-service",
        root_cause=RootCause.RELEASE_500_REGRESSION,
        recommendation=Recommendation.ROLLBACK_RELEASE,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=RELEASE_500_SCENARIO.evidence,
    )

    assert plan.version == 1
    assert plan.status is ActionPlanStatus.DRAFT
    assert plan.risk_level is RiskLevel.HIGH
    assert plan.requires_human_approval is True
    assert len(plan.steps) == 1
    step = plan.steps[0]
    assert step.action_type is ActionType.ROLLBACK_RELEASE
    assert step.target_name == "order-service"
    assert step.parameters.current_version == "2026.01.01-rc1"
    assert step.parameters.target_version == "stable"
    assert step.verification.threshold == 0.01


def test_contested_hypothesis_cannot_generate_plan() -> None:
    with pytest.raises(HypothesisNotSupported, match="contested"):
        propose_action_plan(
            service_name="order-service",
            root_cause=RootCause.RELEASE_500_REGRESSION,
            recommendation=Recommendation.ROLLBACK_RELEASE,
            hypothesis_status=HypothesisStatus.CONTESTED,
            evidence_items=RELEASE_500_SCENARIO.evidence,
        )


def test_release_plan_requires_explicit_previous_and_current_versions() -> None:
    evidence = tuple(
        item for item in RELEASE_500_SCENARIO.evidence if item.kind is not EvidenceKind.CHANGE
    )

    with pytest.raises(UnsafeRollbackTarget, match="缺少有效的发布前后版本"):
        propose_action_plan(
            service_name="order-service",
            root_cause=RootCause.RELEASE_500_REGRESSION,
            recommendation=Recommendation.ROLLBACK_RELEASE,
            hypothesis_status=HypothesisStatus.SUPPORTED,
            evidence_items=evidence,
        )

    change = next(
        item for item in RELEASE_500_SCENARIO.evidence if item.kind is EvidenceKind.CHANGE
    )
    unsafe_change = change.model_copy(
        update={"attributes": {**change.attributes, "from_version": "stable; unsafe"}}
    )
    unsafe_evidence = tuple(
        unsafe_change if item.kind is EvidenceKind.CHANGE else item
        for item in RELEASE_500_SCENARIO.evidence
    )
    with pytest.raises(UnsafeRollbackTarget, match="缺少有效的发布前后版本"):
        propose_action_plan(
            service_name="order-service",
            root_cause=RootCause.RELEASE_500_REGRESSION,
            recommendation=Recommendation.ROLLBACK_RELEASE,
            hypothesis_status=HypothesisStatus.SUPPORTED,
            evidence_items=unsafe_evidence,
        )


def test_recommendation_without_whitelisted_template_is_rejected() -> None:
    with pytest.raises(ActionPlanCapabilityUnavailable, match="尚无经过验证"):
        propose_action_plan(
            service_name="order-service",
            root_cause=RootCause.REDIS_UNAVAILABLE,
            recommendation=Recommendation.RESTORE_DEPENDENCY,
            hypothesis_status=HypothesisStatus.SUPPORTED,
            evidence_items=RELEASE_500_SCENARIO.evidence,
        )
