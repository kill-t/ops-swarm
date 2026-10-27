"""在父任务锁内追加可重放的专业进度事件。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.tasks.artifact_tables import AgentTaskEventRow
from opsswarm.investigation.tasks.contracts import TaskModel

TaskEventType = Literal[
    "run.candidate_settled",
    "run.handed_off",
    "run.human_input_saved",
    "run.cancelled",
    "round.created",
    "round.completed",
    "round.failed",
    "task.started",
    "task.succeeded",
    "task.failed",
    "task.interrupted",
    "task.evidence_saved",
]


class TaskEventRecord(TaskModel):
    investigation_run_id: UUID
    cursor: int
    event_type: TaskEventType
    task_id: UUID | None
    occurred_at: datetime


async def append_task_event(
    session: AsyncSession,
    run_id: UUID,
    *,
    event_type: TaskEventType,
    occurred_at: datetime,
    task_id: UUID | None = None,
) -> None:
    """调用方须已锁定所属 work_items；游标与业务状态按提交顺序递增。"""

    cursor = await session.scalar(
        select(func.coalesce(func.max(AgentTaskEventRow.cursor), 0)).where(
            AgentTaskEventRow.investigation_run_id == run_id
        )
    )
    session.add(
        AgentTaskEventRow(
            investigation_run_id=run_id,
            cursor=int(cursor or 0) + 1,
            event_type=event_type,
            task_id=task_id,
            occurred_at=occurred_at,
        )
    )
