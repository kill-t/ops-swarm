"""保留原轮次、任务和累计账本，区分新增预留与已有调用结算。"""

from collections import Counter
from datetime import datetime, timedelta
from uuid import UUID

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.model_policy import require_model_capacity, summarize_model_usage
from opsswarm.investigation.budget.policy import CallBudgetExceeded, remaining_time
from opsswarm.investigation.reinvestigation_review.contracts import (
    OPERATION,
    ReflowReviewError,
    ReflowReviewPacket,
    ReflowReviewSource,
)
from opsswarm.investigation.review.contracts import HypothesisReviewProposal
from opsswarm.investigation.review.policy import validate_review_evidence
from opsswarm.investigation.synthesis.contracts import OpenHypothesis
from opsswarm.investigation.tasks.errors import TaskFactConflict


def validate_proposal(source: ReflowReviewSource, proposal: HypothesisReviewProposal) -> None:
    ids = {e.evidence_id for p in source.professionals for e in p.evidence.evidence}
    try:
        validate_review_evidence(
            proposal, OpenHypothesis.from_proposal(source.synthesis_result.proposal), ids
        )
    except TaskFactConflict as error:
        raise ReflowReviewError(error.code, str(error)) from error


def check_budget(
    source: ReflowReviewSource,
    packet: ReflowReviewPacket,
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
        raise ReflowReviewError("reflow_review_integrity_violation", "回流缺少精确原预算与完整账本")
    summary = summarize_model_usage(
        source.model_budget,
        [
            (c.model_allocation, c.model_usage)
            for c in budget.calls
            if c.model_allocation is not None
        ],
    )
    if summary != budget.model_usage:
        raise ReflowReviewError("reflow_review_integrity_violation", "回流累计预算与逐次账本不一致")
    remaining_time(source.call_budget, source.started_at, now)
    for call in budget.calls:
        if call.status == "reserved" and not (
            call.id == own_call_id
            and call.operation.value == OPERATION
            and call.kind == "model"
            and getattr(call, "reflow_review_attempt_id", None) is not None
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
    task_tools = Counter(c.scope_task_id for c in budget.calls if c.kind == "tool")
    models = sum(c.kind == "model" for c in budget.calls)
    tools = sum(c.kind == "tool" for c in budget.calls)
    if models + int(allocate) > source.call_budget.max_model_calls:
        raise CallBudgetExceeded("model_call_budget_exceeded")
    if tools > source.call_budget.max_tool_calls or any(
        count > source.call_budget.max_tool_calls_per_task for count in task_tools.values()
    ):
        raise CallBudgetExceeded("tool_call_budget_exceeded")
    require_model_capacity(source.model_budget, summary, packet.allocation if allocate else None)
