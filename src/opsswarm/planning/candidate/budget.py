"""无新外部调用的规划仍受原累计预算约束。"""

from collections import Counter
from datetime import datetime

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.candidate.budget import check_budget
from opsswarm.investigation.candidate.contracts import CandidatePacket, CandidateSource
from opsswarm.planning.candidate.contracts import PlanError


def check_planning_budget(
    source: CandidateSource, packet: CandidatePacket, budget: CallBudgetSnapshot, *, now: datetime
) -> None:
    try:
        task_tools = Counter(
            call.task_id
            for call in budget.calls
            if call.kind == "tool" and call.task_id is not None
        )
        models = sum(call.kind == "model" for call in budget.calls)
        tools = sum(call.kind == "tool" for call in budget.calls)
        if budget.reserved_model_calls != models or budget.reserved_tool_calls != tools:
            raise PlanError("candidate_plan_budget_invalid", "共享预算缺少完整调用次数")
        if (
            models > source.call_budget.max_model_calls
            or tools > source.call_budget.max_tool_calls
            or any(
                count > source.call_budget.max_tool_calls_per_task for count in task_tools.values()
            )
        ):
            raise CallBudgetExceeded("candidate_plan_call_limit_exceeded")
        # 不预留新的模型分配；次数恰好到顶也能生成已有候选的确定性草案。
        check_budget(source, packet, budget, now=now, allocate=False)
    except CallBudgetExceeded as error:
        raise PlanError(error.code, "原累计预算或截止时间不允许生成候选草案") from error
