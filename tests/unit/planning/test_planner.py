"""版本化处置计划生成规则的纯单元测试。"""

import pytest
from pydantic import ValidationError

from opsswarm.investigation.models import (
    EvidenceKind,
    HypothesisStatus,
    Recommendation,
    RootCause,
)
from opsswarm.investigation.scenarios import (
    DATABASE_POOL_SCENARIO_V2,
    REDIS_UNAVAILABLE_SCENARIO_V2,
    RELEASE_500_SCENARIO,
)
from opsswarm.planning import (
    ActionPlanStatus,
    ActionType,
    ConfigurationKey,
    DependencyName,
    HypothesisNotSupported,
    RestoreDependencyParameters,
    RiskLevel,
    RollbackConfigurationParameters,
    RollbackReleaseParameters,
    UnsafeConfigurationRollbackTarget,
    UnsafeDependencyRestoreTarget,
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
    assert plan.risk_policy_version == "action-risk-v3"
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


@pytest.mark.parametrize("invalid_value", [True, "5"])
def test_configuration_parameters_require_strict_integer_values(invalid_value: object) -> None:
    with pytest.raises(ValidationError):
        RollbackConfigurationParameters.model_validate(
            {
                "key": "database_pool_size",
                "current_value": invalid_value,
                "target_value": 50,
            }
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


def test_supported_redis_hypothesis_generates_dependency_restore_draft() -> None:
    plan = propose_action_plan(
        service_name="order-service",
        root_cause=RootCause.REDIS_UNAVAILABLE,
        recommendation=Recommendation.RESTORE_DEPENDENCY,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=REDIS_UNAVAILABLE_SCENARIO_V2.evidence,
    )

    assert plan.status is ActionPlanStatus.DRAFT
    assert plan.risk_level is RiskLevel.HIGH
    assert plan.risk_policy_version == "action-risk-v3"
    step = plan.steps[0]
    assert step.action_type is ActionType.RESTORE_DEPENDENCY
    assert step.target_name == "redis"
    assert isinstance(step.parameters, RestoreDependencyParameters)
    assert step.parameters.dependency is DependencyName.REDIS
    assert step.parameters.expected_available is False
    assert step.parameters.target_available is True
    assert step.verification.signal == "redis_unavailable_ratio"
    assert step.verification.threshold == 0.01


def test_redis_plan_requires_explicit_unhealthy_metric() -> None:
    evidence = tuple(
        item
        for item in REDIS_UNAVAILABLE_SCENARIO_V2.evidence
        if item.kind is not EvidenceKind.METRIC
    )

    with pytest.raises(UnsafeDependencyRestoreTarget, match="Redis 当前不可用"):
        propose_action_plan(
            service_name="order-service",
            root_cause=RootCause.REDIS_UNAVAILABLE,
            recommendation=Recommendation.RESTORE_DEPENDENCY,
            hypothesis_status=HypothesisStatus.SUPPORTED,
            evidence_items=evidence,
        )


@pytest.mark.parametrize("invalid_value", [0, 1, "true"])
def test_dependency_parameters_require_strict_boolean_values(invalid_value: object) -> None:
    with pytest.raises(ValidationError):
        RestoreDependencyParameters.model_validate(
            {
                "dependency": "redis",
                "expected_available": invalid_value,
                "target_available": True,
            }
        )
