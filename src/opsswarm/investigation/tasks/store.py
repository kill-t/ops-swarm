"""在父调查租约保护下原子推进专业任务事实。"""

import hashlib
import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.persistence.tables import (
    InvestigationAttemptRow,
    InvestigationRunRow,
    WorkItemRow,
)
from opsswarm.investigation.tasks.contracts import (
    RoundAssignment,
    RoundStatus,
    TaskOutcome,
    TaskStatus,
)
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.policy import round_status_for_tasks, task_status_after_outcome
from opsswarm.investigation.tasks.records import AttemptStart, TaskAttemptRecord
from opsswarm.investigation.tasks.tables import (
    AgentTaskAttemptRow,
    AgentTaskRow,
    InvestigationRoundRow,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class InvestigationTaskStore:
    """不调用模型或网络；业务任务与 Worker 尝试分别保存。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    @asynccontextmanager
    async def _transaction(
        self,
        run_id: UUID,
        lease_token: UUID,
    ) -> AsyncIterator[tuple[AsyncSession, InvestigationAttemptRow, IncidentRow, datetime]]:
        async with self._session_factory() as session, session.begin():
            work = await session.scalar(
                select(WorkItemRow)
                .where(WorkItemRow.investigation_run_id == run_id)
                .with_for_update()
            )
            now = self._now()
            _require_lease(work, lease_token, now)
            run = await session.get(InvestigationRunRow, run_id)
            if run is None or run.mode != "multi_agent" or run.status != "running":
                raise TaskFactConflict("task_run_not_active", "需要正在运行的多 Agent 调查")
            incident = await session.scalar(
                select(IncidentRow).where(IncidentRow.id == run.incident_id).with_for_update()
            )
            parent = await session.scalar(
                select(InvestigationAttemptRow).where(
                    InvestigationAttemptRow.investigation_run_id == run_id,
                    InvestigationAttemptRow.lease_token == lease_token,
                    InvestigationAttemptRow.status == "running",
                )
            )
            if parent is None or incident is None or incident.status != "investigating":
                raise TaskFactConflict("task_run_not_active", "事故或父调查尝试不再允许写入")
            yield session, parent, incident, now
            # 锁内不进行外部调用，仍在提交前重新检查租约，防止慢查询越过有效期。
            await session.flush()
            _require_lease(work, lease_token, self._now())

    def _now(self) -> datetime:
        now = self._clock()
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("任务时钟必须返回带时区时间")
        return now.astimezone(UTC)

    async def create_round(
        self,
        run_id: UUID,
        *,
        lease_token: UUID,
        assignment: RoundAssignment,
    ) -> tuple[UUID, bool]:
        """整个分派原子创建，重放同一轮不得改变任务范围。"""

        normalized = assignment.model_copy(
            update={"tasks": tuple(sorted(assignment.tasks, key=lambda item: item.task_key))}
        )
        specification = normalized.model_dump(mode="json")
        digest = hashlib.sha256(
            json.dumps(specification, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        async with self._transaction(run_id, lease_token) as (session, _, incident, now):
            previous = list(
                await session.scalars(
                    select(InvestigationRoundRow)
                    .where(InvestigationRoundRow.investigation_run_id == run_id)
                    .order_by(InvestigationRoundRow.round_number)
                )
            )
            existing = next(
                (row for row in previous if row.round_number == assignment.round_number), None
            )
            if existing is not None:
                if existing.content_hash != digest or existing.specification != specification:
                    raise TaskFactConflict("round_content_conflict", "该轮次已绑定另一份分派")
                return existing.id, False
            if assignment.round_number != len(previous) + 1 or (
                previous and previous[-1].status == "running"
            ):
                raise TaskFactConflict("round_sequence_conflict", "上一轮结束后才能顺序创建下一轮")
            if any(task.service_name != incident.service_name for task in assignment.tasks):
                raise TaskFactConflict("task_scope_conflict", "当前切片只允许事故所属服务")
            round_row = InvestigationRoundRow(
                id=uuid4(),
                investigation_run_id=run_id,
                round_number=assignment.round_number,
                goal=assignment.goal,
                specification=specification,
                content_hash=digest,
                status="running",
                created_at=now,
                completed_at=None,
            )
            session.add(round_row)
            await session.flush()
            for task in normalized.tasks:
                session.add(
                    AgentTaskRow(
                        id=uuid4(),
                        investigation_run_id=run_id,
                        round_id=round_row.id,
                        task_key=task.task_key,
                        assignment=task.model_dump(mode="json"),
                        status="pending",
                        version=1,
                        attempt_count=0,
                        max_attempts=task.max_attempts,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                    )
                )
            return round_row.id, True

    async def begin_attempt(
        self,
        run_id: UUID,
        task_id: UUID,
        *,
        lease_token: UUID,
        expected_version: int,
    ) -> AttemptStart:
        """幂等开始；接管过期处理时生成新尝试并保留耗用次数。"""

        async with self._transaction(run_id, lease_token) as (session, parent, _, now):
            task = await _locked_task(session, run_id, task_id)
            latest = await session.scalar(
                select(AgentTaskAttemptRow)
                .where(AgentTaskAttemptRow.task_id == task_id)
                .order_by(AgentTaskAttemptRow.attempt_number.desc())
                .limit(1)
            )
            if (
                latest is not None
                and latest.investigation_attempt_id == parent.id
                and latest.start_version == expected_version
            ):
                return AttemptStart(task_id, _attempt_record(latest), False)
            if task.version != expected_version:
                raise TaskFactConflict("task_version_conflict", "专业任务版本已变化")
            if task.status not in ("pending", "running"):
                raise TaskFactConflict("task_state_conflict", "已结束任务不能再次开始")
            if task.status == "running":
                if latest is None or latest.investigation_attempt_id == parent.id:
                    raise TaskFactConflict("task_state_conflict", "专业任务已经在执行")
                latest.status = "lease_expired"
                latest.finished_at = now
                await session.flush()
            if task.attempt_count >= task.max_attempts:
                task.status = "failed"
                task.version += 1
                task.updated_at = now
                task.completed_at = now
                await _settle_round(session, task.round_id, now)
                return AttemptStart(task_id, None, False)
            attempt = AgentTaskAttemptRow(
                id=uuid4(),
                investigation_run_id=run_id,
                task_id=task_id,
                investigation_attempt_id=parent.id,
                attempt_number=task.attempt_count + 1,
                start_version=task.version,
                status="running",
                outcome=None,
                started_at=now,
                lease_expires_at=parent.lease_expires_at,
                finished_at=None,
            )
            task.attempt_count += 1
            task.version += 1
            task.status = "running"
            task.updated_at = now
            session.add(attempt)
            return AttemptStart(task_id, _attempt_record(attempt), True)

    async def finish_attempt(
        self,
        run_id: UUID,
        task_id: UUID,
        attempt_id: UUID,
        *,
        lease_token: UUID,
        outcome: TaskOutcome,
    ) -> TaskAttemptRecord:
        """在同一事务保存尝试结算、任务状态和轮次终态。"""

        async with self._transaction(run_id, lease_token) as (session, parent, _, now):
            task = await _locked_task(session, run_id, task_id)
            attempt = await session.scalar(
                select(AgentTaskAttemptRow).where(
                    AgentTaskAttemptRow.id == attempt_id,
                    AgentTaskAttemptRow.task_id == task_id,
                    AgentTaskAttemptRow.investigation_run_id == run_id,
                )
            )
            if attempt is None:
                raise TaskFactConflict("task_attempt_not_found", "任务尝试不存在或不属于该运行")
            if attempt.investigation_attempt_id != parent.id:
                raise TaskFactConflict("task_attempt_lease_lost", "不能结算旧 Worker 的专业尝试")
            payload = outcome.model_dump(mode="json")
            if attempt.status != "running":
                if attempt.outcome == payload:
                    return _attempt_record(attempt)
                raise TaskFactConflict("task_outcome_conflict", "尝试已经保存不同的终态")
            if task.status != "running" or task.attempt_count != attempt.attempt_number:
                raise TaskFactConflict("task_state_conflict", "当前任务与尝试状态不一致")
            attempt.status = outcome.status
            attempt.outcome = payload
            attempt.finished_at = now
            task.status = task_status_after_outcome(
                outcome, attempt_count=task.attempt_count, max_attempts=task.max_attempts
            ).value
            task.version += 1
            task.updated_at = now
            task.completed_at = now if task.status in ("succeeded", "failed") else None
            await _settle_round(session, task.round_id, now)
            return _attempt_record(attempt)


def _require_lease(work: WorkItemRow | None, token: UUID, now: datetime) -> None:
    if (
        work is None
        or work.status != "running"
        or work.lease_token != token
        or work.locked_until is None
        or work.locked_until <= now
    ):
        raise TaskFactConflict("task_lease_lost", "调查租约无效，拒绝专业任务写入")


async def _locked_task(session: AsyncSession, run_id: UUID, task_id: UUID) -> AgentTaskRow:
    task = await session.scalar(
        select(AgentTaskRow)
        .where(AgentTaskRow.id == task_id, AgentTaskRow.investigation_run_id == run_id)
        .with_for_update()
    )
    if task is None:
        raise TaskFactConflict("task_not_found", "专业任务不存在或不属于该调查")
    return task


async def _settle_round(session: AsyncSession, round_id: UUID, now: datetime) -> None:
    statuses = list(
        await session.scalars(select(AgentTaskRow.status).where(AgentTaskRow.round_id == round_id))
    )
    status = round_status_for_tasks([TaskStatus(value) for value in statuses])
    if status is not RoundStatus.RUNNING:
        row = await session.get(InvestigationRoundRow, round_id)
        if row is None:
            raise RuntimeError("任务关联轮次缺失")
        row.status = status.value
        row.completed_at = now


def _attempt_record(row: AgentTaskAttemptRow) -> TaskAttemptRecord:
    return TaskAttemptRecord.model_validate(row, from_attributes=True)
