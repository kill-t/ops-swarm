"""从一次连接查询构造有界任务事实快照。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.errors import InvestigationRunNotFound
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.artifact_tables import AgentTaskEventRow, AgentTaskResultRow
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.events import TaskEventRecord
from opsswarm.investigation.tasks.evidence import TaskEvidenceRecord
from opsswarm.investigation.tasks.evidence_store import validated_record
from opsswarm.investigation.tasks.evidence_tables import AgentTaskEvidenceRow
from opsswarm.investigation.tasks.records import (
    CollaborationSnapshot,
    RoundRecord,
    TaskAttemptRecord,
    TaskRecord,
)
from opsswarm.investigation.tasks.results import TaskResultRecord
from opsswarm.investigation.tasks.tables import (
    AgentTaskAttemptRow,
    AgentTaskRow,
    InvestigationRoundRow,
)


class CollaborationQueries:
    """查询不消费租约、不修改状态，也不读取 checkpoint。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_result(self, run_id: UUID, task_id: UUID) -> TaskResultRecord | None:
        async with self._session_factory() as session:
            if await session.get(InvestigationRunRow, run_id) is None:
                raise InvestigationRunNotFound(run_id)
            task = await session.scalar(
                select(AgentTaskRow).where(
                    AgentTaskRow.id == task_id,
                    AgentTaskRow.investigation_run_id == run_id,
                )
            )
            if task is None:
                raise TaskFactConflict("task_not_found", "专业任务不存在或不属于该运行")
            row = await session.get(AgentTaskResultRow, task_id)
            return TaskResultRecord.model_validate(row, from_attributes=True) if row else None

    async def get_evidence(self, run_id: UUID, task_id: UUID) -> TaskEvidenceRecord | None:
        async with self._session_factory() as session:
            if await session.get(InvestigationRunRow, run_id) is None:
                raise InvestigationRunNotFound(run_id)
            task = await session.get(AgentTaskRow, task_id)
            if task is None or task.investigation_run_id != run_id:
                raise TaskFactConflict("task_not_found", "专业任务不存在或不属于该运行")
            row = await session.get(AgentTaskEvidenceRow, task_id)
            return (
                validated_record(row, TaskAssignment.model_validate(task.assignment))
                if row
                else None
            )

    async def list_events(
        self,
        run_id: UUID,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> tuple[TaskEventRecord, ...]:
        if after_cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("事件游标必须非负，每批最多 100 条")
        async with self._session_factory() as session:
            if await session.get(InvestigationRunRow, run_id) is None:
                raise InvestigationRunNotFound(run_id)
            rows = await session.scalars(
                select(AgentTaskEventRow)
                .where(
                    AgentTaskEventRow.investigation_run_id == run_id,
                    AgentTaskEventRow.cursor > after_cursor,
                )
                .order_by(AgentTaskEventRow.cursor)
                .limit(limit)
            )
            return tuple(TaskEventRecord.model_validate(row, from_attributes=True) for row in rows)

    async def get_snapshot(self, run_id: UUID) -> CollaborationSnapshot:
        async with self._session_factory() as session:
            if await session.get(InvestigationRunRow, run_id) is None:
                raise InvestigationRunNotFound(run_id)
            rows = await session.execute(
                select(InvestigationRoundRow, AgentTaskRow, AgentTaskAttemptRow)
                .join(AgentTaskRow, AgentTaskRow.round_id == InvestigationRoundRow.id)
                .outerjoin(AgentTaskAttemptRow, AgentTaskAttemptRow.task_id == AgentTaskRow.id)
                .where(InvestigationRoundRow.investigation_run_id == run_id)
                .order_by(
                    InvestigationRoundRow.round_number,
                    AgentTaskRow.task_key,
                    AgentTaskAttemptRow.attempt_number,
                )
            )
            rounds: dict[UUID, RoundRecord] = {}
            tasks: dict[UUID, TaskRecord] = {}
            attempts: dict[UUID, list[TaskAttemptRecord]] = {}
            for round_row, task_row, attempt_row in rows:
                rounds.setdefault(
                    round_row.id, RoundRecord.model_validate(round_row, from_attributes=True)
                )
                tasks.setdefault(
                    task_row.id, TaskRecord.model_validate(task_row, from_attributes=True)
                )
                if attempt_row is not None:
                    attempts.setdefault(task_row.id, []).append(
                        TaskAttemptRecord.model_validate(attempt_row, from_attributes=True)
                    )
            return CollaborationSnapshot(
                investigation_run_id=run_id,
                rounds=tuple(
                    record.model_copy(
                        update={
                            "tasks": tuple(
                                task.model_copy(
                                    update={"attempts": tuple(attempts.get(task.id, ()))}
                                )
                                for task in tasks.values()
                                if task.round_id == record.id
                            )
                        }
                    )
                    for record in rounds.values()
                ),
            )
