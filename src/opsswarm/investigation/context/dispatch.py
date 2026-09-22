"""按运行固定的策略选择上下文构建器，不隐式升级历史输入。"""

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy, ModelInput
from opsswarm.investigation.context.builder import ContextBuilder
from opsswarm.investigation.context.contracts import ContextPacket, ContextSources
from opsswarm.investigation.context.review_builder import ReviewContextBuilder
from opsswarm.investigation.context.summary_builder import SummaryContextBuilder


def build_context(
    supplied: ModelInput,
    sources: ContextSources,
    operation: CallOperation,
    policy: ModelBudgetPolicy,
) -> ContextPacket:
    if sources.context_policy_version == "summary-context-v1" and operation == (
        CallOperation.REVIEW
    ):
        return ReviewContextBuilder().build(supplied, sources, operation, policy)
    if sources.context_policy_version == "summary-context-v1" and operation == (
        CallOperation.SYNTHESIZE
    ):
        return SummaryContextBuilder().build(supplied, sources, operation, policy)
    return ContextBuilder().build(supplied, sources, operation, policy)
