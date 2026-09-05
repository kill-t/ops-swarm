"""任务事实的只读快照，不暴露租约令牌。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from opsswarm.investigation.tasks.contracts import (
    RoundStatus,
    TaskAssignment,
    TaskAttemptStatus,
    TaskModel,
    TaskOutcome,
    TaskStatus,
)


class TaskAttemptRecord(TaskModel):
    """关联父 Worker 尝试的专业执行历史。"""

    id: UUID
    investigation_run_id: UUID
    task_id: UUID
    investigation_attempt_id: UUID
    attempt_number: int
    start_version: int
    status: TaskAttemptStatus
    outcome: TaskOutcome | None
    started_at: datetime
    lease_expires_at: datetime
    finished_at: datetime | None


class TaskRecord(TaskModel):
    """稳定业务任务和它的多次处理尝试。"""

    id: UUID
    investigation_run_id: UUID
    round_id: UUID
    assignment: TaskAssignment
    status: TaskStatus
    version: int
    attempt_count: int
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    attempts: tuple[TaskAttemptRecord, ...] = ()


class RoundRecord(TaskModel):
    """一个完整分派轮次的业务投影。"""

    id: UUID
    investigation_run_id: UUID
    round_number: int
    goal: str
    status: RoundStatus
    created_at: datetime
    completed_at: datetime | None
    tasks: tuple[TaskRecord, ...] = ()


class CollaborationSnapshot(TaskModel):
    """从业务库读取的有界协作事实，无记录时返回空轮次。"""

    investigation_run_id: UUID
    rounds: tuple[RoundRecord, ...]


@dataclass(frozen=True, slots=True)
class AttemptStart:
    """只有 created 为真才允许发起本次处理。"""

    task_id: UUID
    attempt: TaskAttemptRecord | None
    created: bool
