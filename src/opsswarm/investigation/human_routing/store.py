"""有界补齐与只读查询；路由无需另建模型任务和租约。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

import structlog
from sqlalchemy import exists, func, select, true, tuple_
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.human_prepare.contracts import PreparationError
from opsswarm.investigation.human_review.contracts import HumanReviewError
from opsswarm.investigation.human_review.tables import HumanReviewJobRow
from opsswarm.investigation.human_routing.contracts import HumanRoutingView
from opsswarm.investigation.human_routing.persistence import record_route
from opsswarm.investigation.human_routing.queries import read_route
from opsswarm.investigation.human_routing.tables import HumanReviewRouteRow
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.human_wait.tables import HumanWaitEventRow
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow
from opsswarm.investigation.tasks.errors import TaskFactConflict

logger = structlog.get_logger(__name__)


class HumanRoutingStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime]
    ) -> None:
        self._factory, self._clock = factory, clock
        # 扫描游标只是公平性优化，所有业务事实仍由事务和持久来源决定。
        self._after: tuple[datetime, UUID] | None = None

    async def get(self, run_id: UUID, preparation_id: UUID, job_id: UUID) -> HumanRoutingView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            job = await session.scalar(
                select(HumanReviewJobRow)
                .where(
                    HumanReviewJobRow.id == job_id,
                    HumanReviewJobRow.preparation_job_id == preparation_id,
                    HumanReviewJobRow.investigation_run_id == run_id,
                )
                .with_for_update(read=True)
            )
            if job is None:
                raise HumanReviewError("human_review_job_not_found", "重评任务不存在", 404)
            return await read_route(session, job, run, clock=self._clock)

    async def record_next(self) -> bool:
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                event_count = (
                    select(func.count())
                    .select_from(HumanWaitEventRow)
                    .where(
                        HumanWaitEventRow.investigation_run_id
                        == HumanReviewJobRow.investigation_run_id
                    )
                    .correlate(HumanReviewJobRow)
                    .scalar_subquery()
                )
                candidate = (
                    await session.execute(
                        select(
                            HumanReviewJobRow.id,
                            HumanReviewJobRow.investigation_run_id,
                            HumanReviewJobRow.completed_at,
                        )
                        .join(
                            InvestigationRunRow,
                            InvestigationRunRow.id == HumanReviewJobRow.investigation_run_id,
                        )
                        .join(IncidentRow, IncidentRow.id == InvestigationRunRow.incident_id)
                        .join(
                            WorkItemRow, WorkItemRow.investigation_run_id == InvestigationRunRow.id
                        )
                        .where(
                            HumanReviewJobRow.status == "succeeded",
                            HumanReviewJobRow.id.not_in(skipped),
                            InvestigationRunRow.status == "await_human",
                            IncidentRow.status == "investigating",
                            ~exists(
                                select(CallReservationRow.id).where(
                                    CallReservationRow.investigation_run_id
                                    == InvestigationRunRow.id,
                                    CallReservationRow.status == "reserved",
                                )
                            ),
                            tuple_(HumanReviewJobRow.completed_at, HumanReviewJobRow.id)
                            > self._after
                            if self._after is not None
                            else true(),
                            HumanReviewJobRow.wait_version == event_count + 1,
                            ~exists(
                                select(HumanReviewRouteRow.id).where(
                                    HumanReviewRouteRow.job_id == HumanReviewJobRow.id
                                )
                            ),
                        )
                        .order_by(HumanReviewJobRow.completed_at, HumanReviewJobRow.id)
                        .limit(1)
                        .with_for_update(of=WorkItemRow, skip_locked=True)
                    )
                ).one_or_none()
                if candidate is None:
                    self._after = None
                    return False
                job_id, run_id, completed_at = candidate
                assert completed_at is not None
                self._after = completed_at, job_id
                skipped.append(job_id)
                try:
                    async with session.begin_nested():
                        run = await session.get(InvestigationRunRow, run_id)
                        job = await session.scalar(
                            select(HumanReviewJobRow)
                            .where(HumanReviewJobRow.id == job_id)
                            .with_for_update()
                        )
                        assert run is not None and job is not None
                        if await record_route(session, job, run, clock=self._clock):
                            return True
                except (ValueError, TaskFactConflict) as error:
                    code = (
                        error.code
                        if isinstance(
                            error,
                            (
                                HumanReviewError,
                                PreparationError,
                                HumanWaitError,
                                TaskFactConflict,
                            ),
                        )
                        else "human_routing_source_invalid"
                    )
                    logger.warning(
                        "human_routing_source_rejected", job_id=str(job_id), error_code=code
                    )
        return False
