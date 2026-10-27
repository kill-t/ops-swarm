"""只在原运行账本上判定回流准入，不创建预算或调度任务。"""

from collections import Counter
from datetime import datetime, timedelta
from uuid import UUID

from opsswarm.incidents.convergence.contracts import ConvergenceRecord
from opsswarm.incidents.models import IncidentStatus
from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.model_policy import estimate_cost, summarize_model_usage
from opsswarm.investigation.budget.policy import operation_kind
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.reinvestigation.contracts import (
    ReinvestigationCommand,
    ReinvestigationContent,
    ReinvestigationError,
)


def request_id(run_id: UUID) -> UUID:
    return run_id


def _integrity(message: str) -> ReinvestigationError:
    return ReinvestigationError("reinvestigation_integrity_violation", message)


def _aware(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() is not None


def budget_blockers(budget: CallBudgetSnapshot, *, now: datetime) -> tuple[str, ...]:
    """完整重算账本，未知用量保留原分配，并保留一次协调模型容量。"""
    policy, model = budget.policy, budget.model_budget
    start, deadline = budget.started_at, budget.deadline
    if (
        policy is None
        or model is None
        or budget.model_usage is None
        or start is None
        or deadline is None
        or not all(_aware(value) for value in (start, deadline, now))
        or deadline != start + timedelta(seconds=policy.max_duration_seconds)
        or now < start
    ):
        raise _integrity("原预算策略、时间或用量快照不完整")
    calls = budget.calls
    models = sum(call.kind == "model" for call in calls)
    tools = sum(call.kind == "tool" for call in calls)
    if (
        tuple(call.sequence for call in calls) != tuple(range(1, len(calls) + 1))
        or len({call.id for call in calls}) != len(calls)
        or budget.reserved_model_calls != models
        or budget.reserved_tool_calls != tools
    ):
        raise _integrity("原调用账本序号、身份或累计次数不一致")
    records = []
    usage_overrun = False
    for call in calls:
        allocation, usage = call.model_allocation, call.model_usage
        if (
            call.investigation_run_id != budget.investigation_run_id
            or operation_kind(call.operation) != call.kind
            or not _aware(call.reserved_at)
            or not start <= call.reserved_at < deadline
            or call.reserved_at > now
            or (call.status == "reserved") != (call.finished_at is None)
            or (
                call.finished_at is not None
                and (
                    not _aware(call.finished_at) or not call.reserved_at <= call.finished_at <= now
                )
            )
            or (call.status == "reserved" and usage is not None)
            or (call.kind == "model") != (allocation is not None)
            or (call.kind == "tool" and usage is not None)
        ):
            raise _integrity("调用来源、分类、时间或模型分配不一致")
        if allocation is None:
            continue
        if (
            allocation.estimated_input_tokens != allocation.input_bytes + 256
            or allocation.estimated_input_tokens > model.max_input_tokens_per_call
            or allocation.output_token_limit != model.max_output_tokens_per_call
            or allocation.reserved_cost_usd
            != estimate_cost(
                model.profile, allocation.estimated_input_tokens, allocation.output_token_limit
            )
        ):
            raise _integrity("模型分配与原策略及估算费用不一致")
        records.append((allocation, usage))
        if usage is not None and (
            usage.input_tokens > allocation.estimated_input_tokens
            or usage.output_tokens > allocation.output_token_limit
            or estimate_cost(model.profile, usage.input_tokens, usage.output_tokens)
            > allocation.reserved_cost_usd
        ):
            usage_overrun = True
    summary = summarize_model_usage(model, records)
    if summary != budget.model_usage:
        raise _integrity("累计模型用量与逐次账本重算不一致")
    reasons = []
    if now >= deadline:
        reasons.append("run_deadline_exceeded")
    if any(call.status == "reserved" for call in calls):
        reasons.append("calls_in_flight")
    if models + 1 > policy.max_model_calls:
        reasons.append("model_call_budget_exceeded")
    # 协调模型不调用工具，工具恰好耗尽不妨碍准入，但已超额必须阻断。
    task_tools = Counter(call.task_id for call in calls if call.kind == "tool")
    if tools > policy.max_tool_calls or any(
        count > policy.max_tool_calls_per_task for count in task_tools.values()
    ):
        reasons.append("tool_call_budget_exceeded")
    if summary.committed_input_tokens + model.max_input_tokens_per_call > model.max_input_tokens:
        reasons.append("run_input_budget_exceeded")
    if summary.committed_output_tokens + model.max_output_tokens_per_call > model.max_output_tokens:
        reasons.append("run_output_budget_exceeded")
    cost = estimate_cost(
        model.profile, model.max_input_tokens_per_call, model.max_output_tokens_per_call
    )
    if summary.committed_cost_usd + cost > model.max_estimated_cost_usd:
        reasons.append("model_cost_budget_exceeded")
    if usage_overrun:
        reasons.append("model_usage_exceeded_allocation")
    return tuple(reasons)


def make_content(
    *,
    run_id: UUID,
    incident_id: UUID,
    convergence: ConvergenceRecord,
    budget: CallBudgetSnapshot,
    command: ReinvestigationCommand,
    subject_id: str,
    authorization_id: UUID,
    now: datetime,
) -> ReinvestigationContent:
    if (
        convergence.investigation_run_id != run_id
        or convergence.incident_id != incident_id
        or budget.investigation_run_id != run_id
        or not _aware(now)
        or now < convergence.created_at
    ):
        raise _integrity("准入来源必须属于同一原运行及事故，且不得引用未来事实")
    if (
        convergence.outcome != "observed_failure"
        or convergence.slot_released
        or convergence.after_status != IncidentStatus.INVESTIGATING
    ):
        raise ReinvestigationError(
            "reinvestigation_source_not_eligible", "来源不允许申请验证失败回流"
        )
    if (
        command.convergence_sha256 != convergence.content_sha256
        or command.expected_incident_version != convergence.after_version
    ):
        raise ReinvestigationError(
            "reinvestigation_basis_changed", "命令未绑定精确收敛摘要和事故版本"
        )
    blockers = budget_blockers(budget, now=now)
    if blockers:
        raise ReinvestigationError(blockers[0], "原运行剩余预算不满足一次完整协调模型调用")
    return ReinvestigationContent(
        id=request_id(run_id),
        investigation_run_id=run_id,
        incident_id=incident_id,
        root_run_id=run_id,
        previous_run_id=run_id,
        budget_run_id=run_id,
        convergence_content_sha256=convergence.content_sha256,
        incident_version=convergence.after_version,
        budget=budget,
        subject_id=subject_id,
        authorization_id=authorization_id,
        command=command,
        command_sha256=content_hash(command),
        created_at=now,
    )
