"""无需框架和数据库的调用分类与额度判定。"""

from datetime import datetime, timedelta
from typing import Literal

from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallOperation


class CallBudgetExceeded(Exception):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__("调查调用预算已耗尽")


def operation_kind(operation: CallOperation) -> Literal["model", "tool"]:
    return (
        "tool"
        if operation
        in (
            CallOperation.COLLECT_REINVESTIGATION,
            CallOperation.QUERY_LOGS,
            CallOperation.QUERY_METRICS,
            CallOperation.LIST_CHANGES,
            CallOperation.READ_LOG_EVIDENCE,
            CallOperation.READ_METRIC_EVIDENCE,
            CallOperation.READ_CHANGE_EVIDENCE,
            CallOperation.READ_LOG_RUNBOOK,
            CallOperation.READ_METRIC_RUNBOOK,
            CallOperation.READ_CHANGE_RUNBOOK,
        )
        else "model"
    )


def operation_role(operation: CallOperation) -> str | None:
    return {
        CallOperation.COORDINATE: None,
        CallOperation.COORDINATE_REINVESTIGATION: None,
        CallOperation.COLLECT_REINVESTIGATION: None,
        CallOperation.ANALYZE_REINVESTIGATION: None,
        CallOperation.SYNTHESIZE_REINVESTIGATION: None,
        CallOperation.REVIEW_REINVESTIGATION: None,
        CallOperation.EVALUATE_REINVESTIGATION_CANDIDATE: None,
        CallOperation.QUERY_LOGS: "log",
        CallOperation.ANALYZE_LOG: "log",
        CallOperation.QUERY_METRICS: "metric",
        CallOperation.ANALYZE_METRIC: "metric",
        CallOperation.LIST_CHANGES: "change",
        CallOperation.ANALYZE_CHANGE: "change",
        CallOperation.SYNTHESIZE: None,
        CallOperation.REVIEW: None,
        CallOperation.HUMAN_REVIEW: None,
        CallOperation.EVALUATE_CANDIDATE: None,
        CallOperation.READ_LOG_EVIDENCE: "log",
        CallOperation.READ_METRIC_EVIDENCE: "metric",
        CallOperation.READ_CHANGE_EVIDENCE: "change",
        CallOperation.READ_LOG_RUNBOOK: "log",
        CallOperation.READ_METRIC_RUNBOOK: "metric",
        CallOperation.READ_CHANGE_RUNBOOK: "change",
    }[operation]


def remaining_time(policy: CallBudgetPolicy, started_at: datetime, now: datetime) -> float:
    remaining = (started_at + timedelta(seconds=policy.max_duration_seconds) - now).total_seconds()
    if remaining <= 0:
        raise CallBudgetExceeded("run_deadline_exceeded")
    return remaining


def require_capacity(
    policy: CallBudgetPolicy,
    operation: CallOperation,
    *,
    models: int,
    tools: int,
    task_tools: int,
) -> None:
    if operation_kind(operation) == "model":
        if models >= policy.max_model_calls:
            raise CallBudgetExceeded("model_call_budget_exceeded")
    else:
        if tools >= policy.max_tool_calls:
            raise CallBudgetExceeded("tool_call_budget_exceeded")
        if task_tools >= policy.max_tool_calls_per_task:
            raise CallBudgetExceeded("task_tool_budget_exceeded")
