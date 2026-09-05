"""专业任务的纯输入、重试与轮次状态规则。"""

from datetime import timedelta

import pytest
from pydantic import ValidationError

from opsswarm.investigation.tasks.contracts import (
    RoundAssignment,
    RoundStatus,
    TaskAssignment,
    TaskOutcome,
    TaskStatus,
)
from opsswarm.investigation.tasks.policy import round_status_for_tasks, task_status_after_outcome
from tests.task_facts_helpers import BASE_TIME, assignment


@pytest.mark.parametrize(
    "updates",
    [
        {"role": "planner"},
        {"task_key": "../log"},
        {"goal": "   "},
        {"window_start": BASE_TIME.replace(tzinfo=None)},
        {"max_attempts": 4},
        {"window_end": BASE_TIME - timedelta(days=2)},
        {"window_start": BASE_TIME - timedelta(days=2)},
        {"shell": "anything"},
    ],
)
def test_assignment_rejects_invalid_scope(updates: dict[str, object]) -> None:
    values = assignment().tasks[0].model_dump()
    values.update(updates)
    with pytest.raises(ValidationError):
        TaskAssignment.model_validate(values)


def test_round_rejects_duplicate_or_unbounded_tasks() -> None:
    task = assignment().tasks[0]
    for tasks in ((), (task, task), (task,) * 4):
        with pytest.raises(ValidationError):
            RoundAssignment(round_number=1, goal="调查", tasks=tasks)


@pytest.mark.parametrize(
    "data",
    [
        {"status": "succeeded"},
        {"status": "failed"},
        {"status": "succeeded", "summary": "成功", "retryable": True},
        {"status": "failed", "error_code": "timeout", "summary": "异常文本"},
        {"status": "failed", "error_code": "包含任意响应"},
    ],
)
def test_outcome_rejects_ambiguous_terminal_payload(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TaskOutcome.model_validate(data)


@pytest.mark.parametrize(
    ("retryable", "attempt", "expected"),
    [
        (True, 1, TaskStatus.PENDING),
        (True, 3, TaskStatus.FAILED),
        (False, 1, TaskStatus.FAILED),
    ],
)
def test_failure_respects_retry_limit(retryable: bool, attempt: int, expected: TaskStatus) -> None:
    result = task_status_after_outcome(
        TaskOutcome(status="failed", error_code="source_timeout", retryable=retryable),
        attempt_count=attempt,
        max_attempts=3,
    )
    assert result is expected


def test_round_waits_for_all_branches() -> None:
    assert round_status_for_tasks([TaskStatus.FAILED, TaskStatus.RUNNING]) is RoundStatus.RUNNING
    assert round_status_for_tasks([TaskStatus.SUCCEEDED, TaskStatus.FAILED]) is RoundStatus.FAILED
    assert round_status_for_tasks([TaskStatus.SUCCEEDED]) is RoundStatus.COMPLETED
