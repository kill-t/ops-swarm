"""从不可变133分派重建执行来源，不借用最低容量投影授予运行资格。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError, ReflowSource
from opsswarm.investigation.reinvestigation_coordination.source import check_current_basis
from opsswarm.investigation.reinvestigation_coordination.tables import ReflowJobRow
from opsswarm.investigation.reinvestigation_dispatch.contracts import DispatchContent
from opsswarm.investigation.reinvestigation_dispatch.queries import read_dispatch
from opsswarm.investigation.reinvestigation_dispatch.tables import (
    ReflowDispatchRow,
    ReflowDispatchTaskRow,
)
from opsswarm.investigation.reinvestigation_execution.contracts import EffectiveWindow
from opsswarm.investigation.reinvestigation_execution.tables import ExecutionWorkRow
from opsswarm.investigation.reinvestigation_execution.window import effective_window
from opsswarm.investigation.tasks.contracts import TaskAssignment


@dataclass(frozen=True)
class TaskSource:
    source: ReflowSource
    dispatch: DispatchContent
    dispatch_sha256: str
    assignment: TaskAssignment
    task_id: UUID


async def lock_source(session: AsyncSession, run: InvestigationRunRow) -> None:
    await session.scalar(select(ReflowJobRow).where(ReflowJobRow.id == run.id).with_for_update())
    await session.scalars(
        select(CandidateJobRow)
        .where(CandidateJobRow.investigation_run_id == run.id)
        .order_by(CandidateJobRow.id)
        .with_for_update()
    )
    await session.scalar(
        select(IncidentRow).where(IncidentRow.id == run.incident_id).with_for_update()
    )


async def load_source(
    session: AsyncSession, run: InvestigationRunRow, task_id: UUID, *, clock: Callable[[], datetime]
) -> TaskSource:
    dispatch = await session.get(ReflowDispatchRow, run.id)
    task = await session.get(ReflowDispatchTaskRow, task_id)
    if dispatch is None or task is None or task.investigation_run_id != run.id:
        raise ReflowError("execution_integrity_violation", "执行任务缺少同根133分派")
    view = await read_dispatch(session, run, dispatch, clock=clock)
    if (
        view.record is None
        or view.record.status != "queued"
        or task_id not in {t.id for t in view.tasks}
    ):
        raise ReflowError("execution_integrity_violation", "执行只能消费原queued完整任务")
    job = await session.get(ReflowJobRow, run.id)
    assert job is not None
    return TaskSource(
        ReflowSource.model_validate(job.source),
        view.record,
        dispatch.content_sha256,
        TaskAssignment.model_validate(task.assignment),
        task_id,
    )


def validate_work(work: ExecutionWorkRow, source: TaskSource) -> EffectiveWindow | None:
    if (
        work.task_id != source.task_id
        or work.investigation_run_id != source.source.investigation_run_id
        or work.dispatch_sha256 != source.dispatch_sha256
        or work.assignment_sha256 != content_hash(source.assignment)
        or work.policy_version != "reinvestigation-task-v1"
        or work.evidence_policy_version != "reflow-live-mcp-v1"
        or work.max_attempts != source.assignment.max_attempts
    ):
        raise ReflowError("execution_integrity_violation", "执行身份、版本或分派摘要不一致")
    if work.effective_window is None:
        if work.status != "blocked":
            raise ReflowError("execution_integrity_violation", "可执行任务缺少有效窗口")
        return None
    window = effective_window(source.assignment)
    if window.model_dump(mode="json") != work.effective_window:
        raise ReflowError("execution_integrity_violation", "执行窗口不能由原分派重建")
    return window


async def require_current(
    session: AsyncSession,
    run: InvestigationRunRow,
    work: ExecutionWorkRow,
    *,
    clock: Callable[[], datetime],
) -> TaskSource:
    source = await load_source(session, run, work.task_id, clock=clock)
    validate_work(work, source)
    await check_current_basis(session, run, source.source, now=clock())
    return source
