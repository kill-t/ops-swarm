"""新证据闭包、确定性模板与原累计调用预算。"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.model_policy import require_model_capacity, summarize_model_usage
from opsswarm.investigation.budget.policy import CallBudgetExceeded, remaining_time
from opsswarm.investigation.candidate.contracts import CandidateError
from opsswarm.investigation.candidate.evaluation import evaluate_candidate as evaluate_basis
from opsswarm.investigation.candidate.output import (
    CandidateEvaluation,
    CandidateModelResult,
    CandidateProposal,
)
from opsswarm.investigation.human_wait.contracts import HumanWaitEventRecord
from opsswarm.investigation.models import EvidenceObservation
from opsswarm.investigation.reinvestigation_candidate.context import validate_source
from opsswarm.investigation.reinvestigation_candidate.contracts import (
    OPERATION,
    ReflowCandidateError,
    ReflowCandidatePacket,
    ReflowCandidateSource,
)
from opsswarm.investigation.review.contracts import HypothesisReviewProposal


@dataclass(frozen=True)
class EvaluationBasis:
    service_name: str
    evidence: tuple[EvidenceObservation, ...]
    review: HypothesisReviewProposal
    human_inputs: tuple[HumanWaitEventRecord, ...]


def evaluate_candidate(
    source: ReflowCandidateSource, result: CandidateModelResult
) -> CandidateEvaluation:
    validate_source(source)
    material = next(m for m in source.review_source.materials if m.kind == "human_inputs")
    basis = EvaluationBasis(
        service_name=source.service_name,
        evidence=tuple(e for p in source.review_source.professionals for e in p.evidence.evidence),
        review=source.review_result.proposal,
        human_inputs=tuple(
            HumanWaitEventRecord.model_validate(e) for e in material.content["events"]
        ),
    )
    try:
        return evaluate_basis(basis, result)
    except CandidateError as error:
        raise ReflowCandidateError(error.code, str(error)) from error


def validate_proposal(source: ReflowCandidateSource, proposal: CandidateProposal) -> None:
    evaluate_candidate(source, CandidateModelResult(proposal=proposal))


def check_budget(
    source: ReflowCandidateSource,
    packet: ReflowCandidatePacket,
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
        raise ReflowCandidateError(
            "reflow_candidate_integrity_violation", "回流缺少精确原预算与完整账本"
        )
    summary = summarize_model_usage(
        source.model_budget,
        [
            (c.model_allocation, c.model_usage)
            for c in budget.calls
            if c.model_allocation is not None
        ],
    )
    if summary != budget.model_usage:
        raise ReflowCandidateError(
            "reflow_candidate_integrity_violation", "回流累计预算与逐次账本不一致"
        )
    remaining_time(source.call_budget, source.started_at, now)
    for call in budget.calls:
        if call.status == "reserved" and not (
            call.id == own_call_id
            and call.operation.value == OPERATION
            and call.kind == "model"
            and getattr(call, "reflow_candidate_attempt_id", None) is not None
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
