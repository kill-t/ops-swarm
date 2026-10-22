"""模型返回后保守核对原累计预算，超预留意见不能发布。"""

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.model_policy import require_model_capacity
from opsswarm.investigation.human_review.contracts import HumanReviewError


def require_committed_capacity(budget: CallBudgetSnapshot) -> None:
    if budget.model_budget is None or budget.model_usage is None:
        raise HumanReviewError("human_review_budget_invalid", "重评缺少原模型预算")
    require_model_capacity(budget.model_budget, budget.model_usage)
    if any(
        call.model_usage is not None
        and call.model_allocation is not None
        and (
            call.model_usage.input_tokens > call.model_allocation.estimated_input_tokens
            or call.model_usage.output_tokens > call.model_allocation.output_token_limit
        )
        for call in budget.calls
    ):
        raise HumanReviewError(
            "model_usage_exceeded_allocation", "已报告用量超过预留，停止发布意见"
        )
