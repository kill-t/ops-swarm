"""调查建议到确定性计划模板的能力映射测试。"""

from opsswarm.investigation.models import HypothesisStatus, Recommendation, RootCause
from opsswarm.planning import (
    RESTORE_REDIS_TEMPLATE_VERSION,
    ROLLBACK_CONFIGURATION_TEMPLATE_VERSION,
    ROLLBACK_RELEASE_TEMPLATE_VERSION,
    ActionPlanCapabilityReason,
    ActionPlanCapabilityStatus,
    ActionType,
    assess_action_plan_capability,
)


def test_supported_release_recommendation_exposes_available_template() -> None:
    capability = assess_action_plan_capability(
        root_cause=RootCause.RELEASE_500_REGRESSION,
        recommendation=Recommendation.ROLLBACK_RELEASE,
        hypothesis_status=HypothesisStatus.SUPPORTED,
    )

    assert capability.status is ActionPlanCapabilityStatus.AVAILABLE
    assert capability.action_type is ActionType.ROLLBACK_RELEASE
    assert capability.template_version == ROLLBACK_RELEASE_TEMPLATE_VERSION
    assert capability.requires_human_approval is True
    assert capability.can_create is True
    assert capability.reason_code is ActionPlanCapabilityReason.TEMPLATE_AVAILABLE


def test_database_pool_configuration_recommendation_exposes_available_template() -> None:
    capability = assess_action_plan_capability(
        root_cause=RootCause.DATABASE_POOL_MISCONFIGURATION,
        recommendation=Recommendation.ROLLBACK_CONFIGURATION,
        hypothesis_status=HypothesisStatus.SUPPORTED,
    )

    assert capability.status is ActionPlanCapabilityStatus.AVAILABLE
    assert capability.action_type is ActionType.ROLLBACK_CONFIGURATION
    assert capability.template_version == ROLLBACK_CONFIGURATION_TEMPLATE_VERSION
    assert capability.requires_human_approval is True
    assert capability.can_create is True
    assert capability.reason_code is ActionPlanCapabilityReason.TEMPLATE_AVAILABLE


def test_redis_restore_recommendation_exposes_available_template() -> None:
    capability = assess_action_plan_capability(
        root_cause=RootCause.REDIS_UNAVAILABLE,
        recommendation=Recommendation.RESTORE_DEPENDENCY,
        hypothesis_status=HypothesisStatus.SUPPORTED,
    )

    assert capability.status is ActionPlanCapabilityStatus.AVAILABLE
    assert capability.action_type is ActionType.RESTORE_DEPENDENCY
    assert capability.template_version == RESTORE_REDIS_TEMPLATE_VERSION
    assert capability.requires_human_approval is True
    assert capability.can_create is True
    assert capability.reason_code is ActionPlanCapabilityReason.TEMPLATE_AVAILABLE


def test_continue_investigation_does_not_create_action_plan() -> None:
    capability = assess_action_plan_capability(
        root_cause=RootCause.UNDETERMINED,
        recommendation=Recommendation.CONTINUE_INVESTIGATION,
        hypothesis_status=HypothesisStatus.INSUFFICIENT_EVIDENCE,
    )

    assert capability.status is ActionPlanCapabilityStatus.NOT_APPLICABLE
    assert capability.requires_human_approval is False
    assert capability.can_create is False
    assert capability.reason_code is ActionPlanCapabilityReason.CONTINUE_INVESTIGATION


def test_mismatched_recommendation_cannot_borrow_another_root_cause_template() -> None:
    capability = assess_action_plan_capability(
        root_cause=RootCause.DATABASE_POOL_MISCONFIGURATION,
        recommendation=Recommendation.ROLLBACK_RELEASE,
        hypothesis_status=HypothesisStatus.SUPPORTED,
    )

    assert capability.status is ActionPlanCapabilityStatus.UNSUPPORTED
    assert capability.can_create is False
    assert capability.reason_code is ActionPlanCapabilityReason.RECOMMENDATION_ROOT_CAUSE_MISMATCH


def test_available_template_still_requires_supported_hypothesis() -> None:
    capability = assess_action_plan_capability(
        root_cause=RootCause.RELEASE_500_REGRESSION,
        recommendation=Recommendation.ROLLBACK_RELEASE,
        hypothesis_status=HypothesisStatus.CONTESTED,
    )

    assert capability.status is ActionPlanCapabilityStatus.AVAILABLE
    assert capability.can_create is False
    assert capability.reason_code is ActionPlanCapabilityReason.HYPOTHESIS_NOT_SUPPORTED
