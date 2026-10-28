"""短事务消费协调建议，原子保存独立轮次与整套待执行任务。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.execution.release.observer import utc_now
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_coordination.tables import ReflowJobRow
from opsswarm.investigation.reinvestigation_dispatch.policy import compile_dispatch, task_id
from opsswarm.investigation.reinvestigation_dispatch.source import (
    current_basis,
    event_cursor,
    load_source,
)
from opsswarm.investigation.reinvestigation_dispatch.tables import (
    ReflowDispatchRow,
    ReflowDispatchTaskRow,
)


async def lock_basis(session: AsyncSession, run: InvestigationRunRow) -> None:
    await session.scalars(
        select(CandidateJobRow)
        .where(CandidateJobRow.investigation_run_id == run.id)
        .order_by(CandidateJobRow.id)
        .with_for_update()
    )
    await session.scalar(
        select(IncidentRow).where(IncidentRow.id == run.incident_id).with_for_update()
    )


class DispatchStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ):
        self._factory, self._clock = factory, clock

    async def dispatch_next(self) -> bool:
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                run_id = await session.scalar(
                    select(ReflowJobRow.investigation_run_id)
                    .where(
                        ReflowJobRow.status == "succeeded",
                        ReflowJobRow.investigation_run_id.not_in(
                            select(ReflowDispatchRow.investigation_run_id)
                        ),
                        ReflowJobRow.investigation_run_id.not_in(skipped),
                    )
                    .order_by(ReflowJobRow.completed_at, ReflowJobRow.id)
                    .limit(1)
                )
                if run_id is None:
                    return False
                skipped.append(run_id)
                work = await session.scalar(
                    select(WorkItemRow)
                    .where(WorkItemRow.investigation_run_id == run_id)
                    .with_for_update(skip_locked=True)
                )
                if work is None:
                    continue
                if await session.get(ReflowDispatchRow, run_id) is not None:
                    continue
                job = await session.scalar(
                    select(ReflowJobRow)
                    .where(ReflowJobRow.investigation_run_id == run_id)
                    .with_for_update()
                )
                run = await session.get(InvestigationRunRow, run_id)
                if job is None or run is None:
                    raise ReflowError("dispatch_integrity_violation", "分派缺少原运行或协调阶段")
                await lock_basis(session, run)
                source, result, result_hash, service = await load_source(
                    session, run, job, clock=self._clock
                )
                now = normalized_now(self._clock)
                reason = await current_basis(session, run, source, now)
                budget = await read_budget_snapshot(session, run)
                # 在途状态尚可结算，不将其冻结进要求不可变的历史前缀。
                if any(call.status == "reserved" for call in budget.calls):
                    continue
                content = compile_dispatch(
                    source,
                    result,
                    result_sha256=result_hash,
                    service_name=service,
                    budget=budget,
                    now=now,
                    basis_event_cursor=await event_cursor(session, run),
                    basis_blocker=reason,
                )
                row = ReflowDispatchRow(
                    investigation_run_id=run.id,
                    source_result_sha256=result_hash,
                    status=content.status,
                    content=content.model_dump(mode="json"),
                    content_sha256=content_hash(content),
                    created_at=content.created_at,
                )
                session.add(row)
                await session.flush()
                for assignment in content.assignment.tasks if content.assignment else ():
                    session.add(
                        ReflowDispatchTaskRow(
                            id=task_id(run.id, assignment.task_key),
                            investigation_run_id=run.id,
                            round_id=content.round_id,
                            task_key=assignment.task_key,
                            status="pending",
                            assignment=assignment.model_dump(mode="json"),
                            assignment_sha256=content_hash(assignment),
                            created_at=content.created_at,
                        )
                    )
                await session.flush()
                if content.status == "queued":
                    reason = await current_basis(session, run, source, normalized_now(self._clock))
                    if reason:
                        raise ReflowError(reason, "分派提交前原期限或事故依据失效")
                return True
        return False
