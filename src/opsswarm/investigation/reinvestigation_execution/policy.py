"""专业执行的单次增量预留和累计账本规则，不依赖执行框架。"""

from collections import Counter
from datetime import datetime, timedelta
from uuid import UUID

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.model_contracts import ModelAllocation
from opsswarm.investigation.budget.model_policy import require_model_capacity, summarize_model_usage
from opsswarm.investigation.budget.policy import CallBudgetExceeded, operation_kind, remaining_time
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError, ReflowSource


def check_budget(
    source: ReflowSource,
    budget: CallBudgetSnapshot,
    *,
    task_id: UUID,
    now: datetime,
    reserve: str | None = None,
    allocation: ModelAllocation | None = None,
) -> None:
    policy, model = source.call_budget, source.model_budget
    if (
        budget.investigation_run_id != source.investigation_run_id
        or budget.policy != policy
        or budget.model_budget != model
        or budget.started_at != source.started_at
        or budget.deadline != source.started_at + timedelta(seconds=policy.max_duration_seconds)
        or tuple(c.sequence for c in budget.calls) != tuple(range(1, len(budget.calls) + 1))
        or len({c.id for c in budget.calls}) != len(budget.calls)
        or budget.reserved_model_calls != sum(c.kind == "model" for c in budget.calls)
        or budget.reserved_tool_calls != sum(c.kind == "tool" for c in budget.calls)
        or budget.model_usage is None
    ):
        raise ReflowError("execution_integrity_violation", "专业执行缺少精确原策略和连续完整账本")
    usage = summarize_model_usage(
        model,
        [
            (c.model_allocation, c.model_usage)
            for c in budget.calls
            if c.model_allocation is not None
        ],
    )
    if usage != budget.model_usage:
        raise ReflowError("execution_integrity_violation", "逐次账本与累计用量不一致")
    remaining_time(policy, source.started_at, now)
    for c in budget.calls:
        if (
            c.investigation_run_id != source.investigation_run_id
            or operation_kind(c.operation) != c.kind
            or not source.started_at <= c.reserved_at <= now
            or c.reserved_at >= budget.deadline
            or (c.status == "reserved") != (c.finished_at is None)
            or c.finished_at is not None
            and not c.reserved_at <= c.finished_at <= now
        ):
            raise ReflowError("execution_integrity_violation", "调用范围、分类或时刻不一致")
        if c.status == "reserved" and c.reflow_task_attempt_id is None:
            raise CallBudgetExceeded("calls_in_flight")
        if (
            c.model_usage
            and c.model_allocation
            and (
                c.model_usage.input_tokens > c.model_allocation.estimated_input_tokens
                or c.model_usage.output_tokens > c.model_allocation.output_token_limit
            )
        ):
            raise CallBudgetExceeded("model_usage_exceeded_allocation")
    tools = Counter(c.scope_task_id for c in budget.calls if c.kind == "tool")
    if (
        sum(c.kind == "model" for c in budget.calls) + int(reserve == "analyze_reinvestigation")
        > policy.max_model_calls
    ):
        raise CallBudgetExceeded("model_call_budget_exceeded")
    if (
        sum(tools.values()) + int(reserve == "collect_reinvestigation") > policy.max_tool_calls
        or any(n > policy.max_tool_calls_per_task for n in tools.values())
        or tools[task_id] + int(reserve == "collect_reinvestigation")
        > policy.max_tool_calls_per_task
    ):
        raise CallBudgetExceeded("tool_call_budget_exceeded")
    require_model_capacity(
        model, usage, allocation if reserve == "analyze_reinvestigation" else None
    )
