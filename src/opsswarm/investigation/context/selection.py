"""调查启动时的上下文策略选择与兼容性校验。"""

from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.context.contracts import (
    CONTEXT_POLICY_VERSION,
    CONTEXT_PROMPT_VERSION,
    ContextPolicyVersion,
)
from opsswarm.investigation.loop.contracts import LOOP_POLICY_VERSION, LOOP_PROMPT_VERSION
from opsswarm.investigation.workflow_versions import WorkflowVersion


def resolve_context_policy(
    workflow_version: WorkflowVersion,
    model_budget: ModelBudgetPolicy | None,
    requested: ContextPolicyVersion | None,
) -> ContextPolicyVersion | None:
    """保留省略字段的旧行为，并校验显式策略所需能力。"""

    if requested is None:
        if model_budget is None:
            return None
        if workflow_version is not WorkflowVersion.V2:
            raise ValueError("只有 v2 调查允许配置模型预算")
        return CONTEXT_POLICY_VERSION

    if workflow_version is not WorkflowVersion.V2:
        raise ValueError("显式上下文策略只支持 v2 调查")
    if model_budget is None:
        raise ValueError("显式上下文策略必须配置模型预算")
    if (
        requested == LOOP_POLICY_VERSION
        and model_budget.profile.structured_output_method != "function_calling"
    ):
        raise ValueError("evidence-read-v1 必须使用 function_calling 模型 profile")
    return requested


def select_context_prompt(
    context_policy_version: ContextPolicyVersion | None,
    fallback: str,
) -> str:
    """让运行固定的提示版本与最终选中的策略一致。"""

    if context_policy_version == LOOP_POLICY_VERSION:
        return LOOP_PROMPT_VERSION
    if context_policy_version == CONTEXT_POLICY_VERSION:
        return CONTEXT_PROMPT_VERSION
    return fallback
