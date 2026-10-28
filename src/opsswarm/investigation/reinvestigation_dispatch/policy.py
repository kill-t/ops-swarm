"""把协调建议编译为原容量内、仅针对验证后窗口的只读任务。"""

import hashlib
from datetime import datetime, timedelta
from uuid import UUID

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.model_policy import estimate_cost
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.reinvestigation.policy import budget_blockers
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowResult, ReflowSource
from opsswarm.investigation.reinvestigation_coordination.policy import validate_proposal
from opsswarm.investigation.reinvestigation_dispatch.contracts import DispatchContent
from opsswarm.investigation.tasks.contracts import RoundAssignment, TaskAssignment


def stable_id(run_id: UUID, name: str) -> UUID:
    """以完整根身份及版本化名称生成独立的SHA256前128位业务身份。"""
    return UUID(hex=hashlib.sha256(f"{run_id}:{name}".encode()).hexdigest()[:32])


def task_id(run_id: UUID, key: str) -> UUID:
    return stable_id(run_id, f"reflow-task-v1:{key}")


def capacity_blocker(budget: CallBudgetSnapshot, count: int, *, now: datetime) -> str | None:
    # 复用已验收的账本完整性重算，专业任务最低容量按每任务一次模型/工具计。
    reasons = set(budget_blockers(budget, now=now))
    policy, model, usage = budget.policy, budget.model_budget, budget.model_usage
    assert policy is not None and model is not None and usage is not None
    assert budget.reserved_model_calls is not None and budget.reserved_tool_calls is not None
    if budget.reserved_model_calls + count > policy.max_model_calls:
        reasons.add("model_call_budget_exceeded")
    if budget.reserved_tool_calls + count > policy.max_tool_calls:
        reasons.add("tool_call_budget_exceeded")
    if (
        usage.committed_input_tokens + count * model.max_input_tokens_per_call
        > model.max_input_tokens
    ):
        reasons.add("run_input_budget_exceeded")
    if (
        usage.committed_output_tokens + count * model.max_output_tokens_per_call
        > model.max_output_tokens
    ):
        reasons.add("run_output_budget_exceeded")
    cost = estimate_cost(
        model.profile, model.max_input_tokens_per_call, model.max_output_tokens_per_call
    )
    if usage.committed_cost_usd + count * cost > model.max_estimated_cost_usd:
        reasons.add("model_cost_budget_exceeded")
    order = (
        "run_deadline_exceeded",
        "calls_in_flight",
        "model_call_budget_exceeded",
        "tool_call_budget_exceeded",
        "run_input_budget_exceeded",
        "run_output_budget_exceeded",
        "model_cost_budget_exceeded",
        "model_usage_exceeded_allocation",
    )
    return next((code for code in order if code in reasons), None)


def compile_dispatch(
    source: ReflowSource,
    result: ReflowResult,
    *,
    result_sha256: str,
    service_name: str,
    budget: CallBudgetSnapshot,
    now: datetime,
    basis_event_cursor: int,
    basis_blocker: str | None = None,
) -> DispatchContent:
    validate_proposal(source, result.proposal)
    if (
        result.investigation_run_id != source.investigation_run_id
        or result.job_id != source.investigation_run_id
        or budget.investigation_run_id != source.investigation_run_id
        or budget.policy != source.call_budget
        or budget.model_budget != source.model_budget
        or budget.started_at != source.started_at
        or now.tzinfo is None
        or now.utcoffset() is None
        or now < result.created_at
        or result.created_at < source.created_at
        or result.source_sha256 != content_hash(source)
        or result_sha256 != content_hash(result)
        or source.maximum_tasks
        != max(
            0,
            min(
                source.coordination_policy.max_tasks_per_round,
                source.coordination_policy.max_total_tasks - source.existing_task_count,
            ),
        )
    ):
        raise ValueError("回流分派必须绑定精确原运行、策略与已完成协调结果")
    material = next(m for m in source.materials if m.kind == "recovery")
    floor = datetime.fromisoformat(material.content["result"]["created_at"])
    if floor.tzinfo is None or floor.utcoffset() is None or floor > result.created_at:
        raise ValueError("失败验证时刻必须有效且不晚于协调结果")
    reason = basis_blocker
    if reason is None and any(
        t.lookback_seconds > (21600 if t.role.value == "change" else 900)
        for t in result.proposal.tasks
    ):
        reason = "reflow_task_window_exceeded"
    if reason is None and now <= floor:
        reason = "reflow_window_not_ready"
    if reason is None:
        reason = capacity_blocker(budget, len(result.proposal.tasks), now=now)
    assignment = None
    if reason is None:
        assignment = RoundAssignment(
            round_number=result.proposal.round_number,
            goal=result.proposal.goal,
            tasks=tuple(
                TaskAssignment(
                    task_key=t.task_key,
                    role=t.role,
                    goal=t.goal,
                    service_name=service_name,
                    window_start=max(floor, now - timedelta(seconds=t.lookback_seconds)),
                    window_end=now,
                    max_attempts=source.coordination_policy.max_attempts_per_task,
                    evidence_snapshot_id=None,
                )
                for t in sorted(result.proposal.tasks, key=lambda t: t.task_key)
            ),
        )
    return DispatchContent(
        investigation_run_id=source.investigation_run_id,
        source_result_sha256=result_sha256,
        source_sha256=result.source_sha256,
        source_call_id=result.source_call_id,
        service_name=service_name,
        basis_event_cursor=basis_event_cursor,
        observation_floor_at=floor,
        budget=budget,
        status="blocked" if reason else "queued",
        reason_code=reason,
        round_id=stable_id(source.investigation_run_id, "reflow-round-v1") if not reason else None,
        assignment=assignment,
        created_at=now,
    )
