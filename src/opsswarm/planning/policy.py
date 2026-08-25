"""不依赖模型的动作风险分级策略。"""

from opsswarm.planning.models import (
    ActionPlan,
    ActionStep,
    ActionType,
    RiskDecision,
    RiskLevel,
    RollbackReleaseParameters,
)

RISK_POLICY_VERSION = "action-risk-v1"


def classify_action_risk(
    action_type: ActionType,
    parameters: RollbackReleaseParameters,
) -> RiskDecision:
    """根据动作和经过白名单校验的参数确定风险，不采信模型自报等级。"""

    if action_type is ActionType.ROLLBACK_RELEASE:
        return RiskDecision(
            risk_level=RiskLevel.HIGH,
            requires_human_approval=True,
            policy_version=RISK_POLICY_VERSION,
            rule_code="rollback_changes_running_version",
            rationale=(
                f"回滚会把运行版本从 {parameters.current_version} 改为 "
                f"{parameters.target_version}，必须由工程师审批。"
            ),
        )
    raise ValueError(f"风险策略不支持动作：{action_type}")


def validate_action_step_risk(step: ActionStep, *, policy_version: str) -> None:
    """重新计算步骤风险，拒绝伪造的低风险或免审批结果。"""

    expected = classify_action_risk(step.action_type, step.parameters)
    if policy_version != expected.policy_version:
        raise ValueError("处置计划使用了未知或过期的风险策略版本")
    actual = (
        step.risk_level,
        step.requires_human_approval,
        step.risk_rule_code,
        step.risk_rationale,
    )
    expected_values = (
        expected.risk_level,
        expected.requires_human_approval,
        expected.rule_code,
        expected.rationale,
    )
    if actual != expected_values:
        raise ValueError("处置步骤风险与确定性策略重算结果不一致")


def validate_action_plan_policy(plan: ActionPlan) -> None:
    """持久化前验证计划中的每个动作和整体策略版本。"""

    for step in plan.steps:
        validate_action_step_risk(step, policy_version=plan.risk_policy_version)
