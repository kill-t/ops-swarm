"""保留原轮次、任务和累计账本，区分新增预留与已有调用结算。"""

from collections import Counter
from datetime import datetime, timedelta
from uuid import UUID

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.model_policy import require_model_capacity, summarize_model_usage
from opsswarm.investigation.budget.policy import CallBudgetExceeded, remaining_time
from opsswarm.investigation.coordination.contracts import CoordinationProposal
from opsswarm.investigation.reinvestigation_coordination.contracts import (
    OPERATION,
    ReflowError,
    ReflowPacket,
    ReflowSource,
)


def prepare_blocker(source: ReflowSource) -> str | None:
    if source.next_round_number > source.coordination_policy.max_rounds:
        return "reflow_round_limit_exceeded"
    if source.maximum_tasks < 1:
        return "reflow_task_limit_exceeded"
    return None


def validate_proposal(source: ReflowSource, proposal: CoordinationProposal) -> None:
    if prepare_blocker(source) or proposal.round_number != source.next_round_number:
        raise ReflowError("reflow_proposal_invalid", "建议轮次不能重置或超过原轮次容量")
    if len(proposal.tasks) > source.maximum_tasks:
        raise ReflowError("reflow_proposal_invalid", "建议任务超过原剩余任务容量")


def check_budget(
    source: ReflowSource,
    packet: ReflowPacket,
    budget: CallBudgetSnapshot,
    *,
    now: datetime,
    allocate: bool,
    own_call_id: UUID | None = None,
) -> None:
    if (
        budget.investigation_run_id != source.investigation_run_id
        or budget.policy != source.call_budget
        or budget.model_budget != source.model_budget
        or budget.started_at != source.started_at
        or budget.deadline
        != source.started_at + timedelta(seconds=source.call_budget.max_duration_seconds)
        or budget.model_usage is None
        or tuple(c.sequence for c in budget.calls) != tuple(range(1, len(budget.calls) + 1))
        or budget.reserved_model_calls != sum(c.kind == "model" for c in budget.calls)
        or budget.reserved_tool_calls != sum(c.kind == "tool" for c in budget.calls)
        or any(c.investigation_run_id != source.investigation_run_id for c in budget.calls)
    ):
        raise ReflowError("reflow_integrity_violation", "回流缺少精确原预算与完整账本")
    summary = summarize_model_usage(
        source.model_budget,
        [
            (c.model_allocation, c.model_usage)
            for c in budget.calls
            if c.model_allocation is not None
        ],
    )
    if summary != budget.model_usage:
        raise ReflowError("reflow_integrity_violation", "回流累计预算与逐次账本不一致")
    remaining_time(source.call_budget, source.started_at, now)
    for call in budget.calls:
        if call.status == "reserved" and not (
            call.id == own_call_id
            and call.operation.value == OPERATION
            and call.kind == "model"
            and getattr(call, "reinvestigation_attempt_id", None) is not None
            and call.investigation_attempt_id == source.parent_attempt_id
            and call.model_allocation == packet.allocation
        ):
            raise CallBudgetExceeded("calls_in_flight")
        if (
            call.model_usage
            and call.model_allocation
            and (
                call.model_usage.input_tokens > call.model_allocation.estimated_input_tokens
                or call.model_usage.output_tokens > call.model_allocation.output_token_limit
            )
        ):
            raise CallBudgetExceeded("model_usage_exceeded_allocation")
    task_tools = Counter(c.task_id for c in budget.calls if c.kind == "tool")
    models = sum(c.kind == "model" for c in budget.calls)
    tools = sum(c.kind == "tool" for c in budget.calls)
    if models + int(allocate) > source.call_budget.max_model_calls:
        raise CallBudgetExceeded("model_call_budget_exceeded")
    if tools > source.call_budget.max_tool_calls or any(
        count > source.call_budget.max_tool_calls_per_task for count in task_tools.values()
    ):
        raise CallBudgetExceeded("tool_call_budget_exceeded")
    require_model_capacity(source.model_budget, summary, packet.allocation if allocate else None)
