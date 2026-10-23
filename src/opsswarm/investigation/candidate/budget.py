"""独立候选阶段复核原账本，不创建第二套预算。"""

from datetime import datetime

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot, CallOperation
from opsswarm.investigation.budget.model_policy import require_model_capacity
from opsswarm.investigation.budget.policy import (
    CallBudgetExceeded,
    remaining_time,
    require_capacity,
)
from opsswarm.investigation.candidate.contracts import (
    CandidateError,
    CandidatePacket,
    CandidateSource,
)


def check_budget(
    source: CandidateSource,
    packet: CandidatePacket,
    budget: CallBudgetSnapshot,
    *,
    now: datetime,
    allocate: bool,
) -> None:
    if (
        budget.policy != source.call_budget
        or budget.model_budget != source.model_budget
        or budget.started_at != source.started_at
        or budget.investigation_run_id != source.handoff.investigation_run_id
        or budget.model_usage is None
    ):
        raise CandidateError("candidate_budget_invalid", "候选阶段缺少匹配的原共享预算")
    remaining_time(source.call_budget, source.started_at, now)
    if any(
        c.model_usage
        and c.model_allocation
        and (
            c.model_usage.input_tokens > c.model_allocation.estimated_input_tokens
            or c.model_usage.output_tokens > c.model_allocation.output_token_limit
        )
        for c in budget.calls
    ):
        raise CallBudgetExceeded("model_usage_exceeded_allocation")
    if allocate:
        if any(c.status == "reserved" for c in budget.calls):
            raise CallBudgetExceeded("calls_in_flight")
        require_capacity(
            source.call_budget,
            CallOperation.EVALUATE_CANDIDATE,
            models=sum(c.kind == "model" for c in budget.calls),
            tools=sum(c.kind == "tool" for c in budget.calls),
            task_tools=0,
        )
    require_model_capacity(
        source.model_budget, budget.model_usage, packet.allocation if allocate else None
    )
