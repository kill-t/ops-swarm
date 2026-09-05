"""任务失败重试和轮次结束的确定性策略。"""

from collections.abc import Sequence

from opsswarm.investigation.tasks.contracts import RoundStatus, TaskOutcome, TaskStatus


def task_status_after_outcome(
    outcome: TaskOutcome, *, attempt_count: int, max_attempts: int
) -> TaskStatus:
    """重试只能消耗剩余额度，不能通过失败重置次数。"""

    if not 1 <= attempt_count <= max_attempts <= 3:
        raise ValueError("任务尝试计数不合法")
    if outcome.status == "succeeded":
        return TaskStatus.SUCCEEDED
    if outcome.retryable and attempt_count < max_attempts:
        return TaskStatus.PENDING
    return TaskStatus.FAILED


def round_status_for_tasks(statuses: Sequence[TaskStatus]) -> RoundStatus:
    """并行分支仍有工作时保留轮次，等待所有分支结算。"""

    if not statuses:
        raise ValueError("轮次必须至少包含一个任务")
    if any(status in (TaskStatus.PENDING, TaskStatus.RUNNING) for status in statuses):
        return RoundStatus.RUNNING
    if all(status is TaskStatus.SUCCEEDED for status in statuses):
        return RoundStatus.COMPLETED
    return RoundStatus.FAILED
