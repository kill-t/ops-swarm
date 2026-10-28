"""重建不可变分派和整套业务任务，当前失效不改写历史。"""

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.execution.release.observer import utc_now
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation.queries import historical_budget
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_coordination.tables import ReflowJobRow
from opsswarm.investigation.reinvestigation_dispatch.contracts import (
    DispatchContent,
    DispatchTask,
    DispatchView,
)
from opsswarm.investigation.reinvestigation_dispatch.policy import (
    capacity_blocker,
    compile_dispatch,
    task_id,
)
from opsswarm.investigation.reinvestigation_dispatch.source import (
    current_basis,
    historical_basis,
    load_source,
)
from opsswarm.investigation.reinvestigation_dispatch.tables import (
    ReflowDispatchRow,
    ReflowDispatchTaskRow,
)


async def read_dispatch(
    session: AsyncSession,
    run: InvestigationRunRow,
    row: ReflowDispatchRow,
    *,
    clock: Callable[[], datetime],
) -> DispatchView:
    try:
        job = await session.get(ReflowJobRow, run.id)
        if job is None:
            raise ValueError("分派缺少132来源")
        source, result, result_hash, service = await load_source(session, run, job, clock=clock)
        content = DispatchContent.model_validate(row.content)
        current_budget = await read_budget_snapshot(session, run)
        captured = historical_budget(current_budget, content.budget, content.created_at)
        historical_reason = await historical_basis(session, run, source, content.basis_event_cursor)
        expected = compile_dispatch(
            source,
            result,
            result_sha256=result_hash,
            service_name=service,
            budget=captured,
            now=content.created_at,
            basis_event_cursor=content.basis_event_cursor,
            basis_blocker=historical_reason,
        )
        if (
            content != expected
            or row.investigation_run_id != run.id
            or row.status != content.status
            or row.source_result_sha256 != result_hash
            or row.created_at != content.created_at
            or row.content_sha256 != content_hash(content)
            or row.content_sha256 != raw_content_hash(row.content)
        ):
            raise ValueError("分派正文、摘要或来源不一致")
        tasks = list(
            await session.scalars(
                select(ReflowDispatchTaskRow)
                .where(ReflowDispatchTaskRow.investigation_run_id == run.id)
                .order_by(ReflowDispatchTaskRow.task_key)
            )
        )
        expected_tasks = content.assignment.tasks if content.assignment else ()
        if len(tasks) != len(expected_tasks):
            raise ValueError("分派任务缺失或被追加")
        views = []
        for task, assignment in zip(tasks, expected_tasks, strict=True):
            if (
                task.id != task_id(run.id, assignment.task_key)
                or task.round_id != content.round_id
                or task.task_key != assignment.task_key
                or task.status != "pending"
                or task.assignment != assignment.model_dump(mode="json")
                or task.assignment_sha256 != content_hash(assignment)
                or task.assignment_sha256 != raw_content_hash(task.assignment)
                or task.created_at != content.created_at
            ):
                raise ValueError("分派任务身份或完整范围不一致")
            views.append(DispatchTask.model_validate(task, from_attributes=True))
        reason = await current_basis(session, run, source, clock())
        availability: Literal["current", "stale", "expired", "blocked"]
        if reason == "reflow_source_changed":
            availability = "stale"
        elif reason == "run_deadline_exceeded":
            availability = "expired"
        else:
            reason = (
                content.reason_code
                if content.status == "blocked"
                else capacity_blocker(current_budget, len(expected_tasks), now=clock())
            )
            availability = "blocked" if reason else "current"
        return DispatchView(
            record=content,
            tasks=tuple(views),
            availability=availability,
            blocking_reason_codes=(reason,) if reason else (),
        )
    except (ValueError, TypeError) as error:
        raise ReflowError(
            "dispatch_integrity_violation", "回流分派历史无法从权威来源重建"
        ) from error


class DispatchQueries:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ):
        self._factory, self._clock = factory, clock

    async def get(self, run_id: UUID) -> DispatchView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            await session.scalar(
                select(IncidentRow)
                .where(IncidentRow.id == run.incident_id)
                .with_for_update(read=True)
            )
            row = await session.get(ReflowDispatchRow, run_id)
            if row is None:
                return DispatchView(
                    record=None, tasks=(), availability="pending", blocking_reason_codes=()
                )
            return await read_dispatch(session, run, row, clock=self._clock)
