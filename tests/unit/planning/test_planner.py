"""版本化处置计划生成规则的纯单元测试。"""

import pytest

from opsswarm.investigation.models import (
    EvidenceKind,
    HypothesisStatus,
    Recommendation,
    RootCause,
)
from opsswarm.investigation.scenarios import DATABASE_POOL_SCENARIO_V2, RELEASE_500_SCENARIO
from opsswarm.planning import (
    ActionPlanCapabilityUnavailable,
    ActionPlanStatus,
    ActionType,
    ConfigurationKey,
    HypothesisNotSupported,
    RiskLevel,
    RollbackConfigurationParameters,
    RollbackReleaseParameters,
    UnsafeConfigurationRollbackTarget,
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
    assert isinstance(step.parameters, RollbackReleaseParameters)
    assert step.parameters.current_version == "2026.01.01-rc1"
    assert step.parameters.target_version == "stable"
    assert step.verification.threshold == 0.01


def test_supported_database_pool_hypothesis_generates_configuration_rollback_draft() -> None:
    plan = propose_action_plan(
        service_name="order-service",
        root_cause=RootCause.DATABASE_POOL_MISCONFIGURATION,
        recommendation=Recommendation.ROLLBACK_CONFIGURATION,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=DATABASE_POOL_SCENARIO_V2.evidence,
    )

    assert plan.status is ActionPlanStatus.DRAFT
    assert plan.risk_level is RiskLevel.HIGH
    assert plan.requires_human_approval is True
    assert plan.risk_policy_version == "action-risk-v2"
    step = plan.steps[0]
    assert step.action_type is ActionType.ROLLBACK_CONFIGURATION
    assert isinstance(step.parameters, RollbackConfigurationParameters)
    assert step.parameters.key is ConfigurationKey.DATABASE_POOL_SIZE
    assert step.parameters.current_value == 5
    assert step.parameters.target_value == 50
    assert step.verification.signal == "database_pool_saturation"
    assert step.verification.threshold == 0.8


def test_configuration_plan_requires_whitelisted_previous_and_current_values() -> None:
    evidence = tuple(
        item for item in DATABASE_POOL_SCENARIO_V2.evidence if item.kind is not EvidenceKind.CHANGE
    )

    with pytest.raises(UnsafeConfigurationRollbackTarget, match="连接池配置前后值"):
        propose_action_plan(
            service_name="order-service",
            root_cause=RootCause.DATABASE_POOL_MISCONFIGURATION,
            recommendation=Recommendation.ROLLBACK_CONFIGURATION,
            hypothesis_status=HypothesisStatus.SUPPORTED,
            evidence_items=evidence,
        )


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
