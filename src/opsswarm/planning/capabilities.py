"""调查建议到确定性 Action Plan 模板的显式能力映射。"""

from opsswarm.investigation.models import HypothesisStatus, Recommendation, RootCause
from opsswarm.planning.models import (
    ActionPlanCapability,
    ActionPlanCapabilityReason,
    ActionPlanCapabilityStatus,
    ActionType,
)

ROLLBACK_RELEASE_TEMPLATE_VERSION = "rollback-release-v1"

_EXPECTED_RECOMMENDATION_BY_ROOT_CAUSE = {
    RootCause.RELEASE_500_REGRESSION: Recommendation.ROLLBACK_RELEASE,
    RootCause.DATABASE_POOL_MISCONFIGURATION: Recommendation.ROLLBACK_CONFIGURATION,
    RootCause.REDIS_UNAVAILABLE: Recommendation.RESTORE_DEPENDENCY,
    RootCause.UNDETERMINED: Recommendation.CONTINUE_INVESTIGATION,
}


def assess_action_plan_capability(
    *,
    root_cause: RootCause,
    recommendation: Recommendation,
    hypothesis_status: HypothesisStatus,
) -> ActionPlanCapability:
    """只依据冻结事实判断模板能力，不调用模型也不执行动作。"""

    if recommendation is Recommendation.CONTINUE_INVESTIGATION:
        return _capability(
            root_cause=root_cause,
            recommendation=recommendation,
            hypothesis_status=hypothesis_status,
            status=ActionPlanCapabilityStatus.NOT_APPLICABLE,
            reason_code=ActionPlanCapabilityReason.CONTINUE_INVESTIGATION,
            explanation="调查结论要求继续收集证据，不应生成处置计划。",
        )

    expected = _EXPECTED_RECOMMENDATION_BY_ROOT_CAUSE[root_cause]
    if recommendation is not expected:
        return _capability(
            root_cause=root_cause,
            recommendation=recommendation,
            hypothesis_status=hypothesis_status,
            status=ActionPlanCapabilityStatus.UNSUPPORTED,
            reason_code=ActionPlanCapabilityReason.RECOMMENDATION_ROOT_CAUSE_MISMATCH,
            explanation="调查建议与根因类型不匹配，禁止套用其他处置模板。",
        )

    if (
        root_cause is RootCause.RELEASE_500_REGRESSION
        and recommendation is Recommendation.ROLLBACK_RELEASE
    ):
        can_create = hypothesis_status is HypothesisStatus.SUPPORTED
        return _capability(
            root_cause=root_cause,
            recommendation=recommendation,
            hypothesis_status=hypothesis_status,
            status=ActionPlanCapabilityStatus.AVAILABLE,
            action_type=ActionType.ROLLBACK_RELEASE,
            template_version=ROLLBACK_RELEASE_TEMPLATE_VERSION,
            reason_code=(
                ActionPlanCapabilityReason.TEMPLATE_AVAILABLE
                if can_create
                else ActionPlanCapabilityReason.HYPOTHESIS_NOT_SUPPORTED
            ),
            explanation=(
                "版本回滚模板可用，且主根因假设已经通过确定性质疑。"
                if can_create
                else "版本回滚模板可用，但主根因假设尚未通过确定性质疑。"
            ),
        )

    return _capability(
        root_cause=root_cause,
        recommendation=recommendation,
        hypothesis_status=hypothesis_status,
        status=ActionPlanCapabilityStatus.UNSUPPORTED,
        reason_code=ActionPlanCapabilityReason.TEMPLATE_NOT_IMPLEMENTED,
        explanation="建议语义已识别，但当前版本尚无经过验证的确定性计划模板。",
    )


def _capability(
    *,
    root_cause: RootCause,
    recommendation: Recommendation,
    hypothesis_status: HypothesisStatus,
    status: ActionPlanCapabilityStatus,
    reason_code: ActionPlanCapabilityReason,
    explanation: str,
    action_type: ActionType | None = None,
    template_version: str | None = None,
) -> ActionPlanCapability:
    return ActionPlanCapability(
        root_cause=root_cause,
        recommendation=recommendation,
        hypothesis_status=hypothesis_status,
        status=status,
        action_type=action_type,
        template_version=template_version,
        requires_human_approval=recommendation.requires_human_approval,
        can_create=(
            status is ActionPlanCapabilityStatus.AVAILABLE
            and hypothesis_status is HypothesisStatus.SUPPORTED
        ),
        reason_code=reason_code,
        explanation=explanation,
    )
