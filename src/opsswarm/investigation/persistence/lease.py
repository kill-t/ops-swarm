"""专业任务与调用预算共用的有效父租约事务边界。"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.persistence.tables import (
    InvestigationAttemptRow,
    InvestigationRunRow,
    WorkItemRow,
)
from opsswarm.investigation.tasks.errors import TaskFactConflict


def normalized_now(clock: Callable[[], datetime]) -> datetime:
    now = clock()
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("任务时钟必须返回带时区时间")
    return now.astimezone(UTC)


def require_lease(work: WorkItemRow | None, token: UUID, now: datetime) -> None:
    if (
        work is None
        or work.status != "running"
        or work.lease_token != token
        or work.locked_until is None
        or work.locked_until <= now
    ):
        raise TaskFactConflict("task_lease_lost", "调查租约无效，拒绝专业任务写入")


@asynccontextmanager
async def active_investigation_transaction(
    factory: async_sessionmaker[AsyncSession],
    run_id: UUID,
    lease_token: UUID,
    clock: Callable[[], datetime],
) -> AsyncIterator[
    tuple[AsyncSession, InvestigationAttemptRow, IncidentRow, InvestigationRunRow, datetime]
]:
    async with factory() as session, session.begin():
        work = await session.scalar(
            select(WorkItemRow).where(WorkItemRow.investigation_run_id == run_id).with_for_update()
        )
        now = normalized_now(clock)
        require_lease(work, lease_token, now)
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
        yield session, parent, incident, run, now
        await session.flush()
        require_lease(work, lease_token, normalized_now(clock))
