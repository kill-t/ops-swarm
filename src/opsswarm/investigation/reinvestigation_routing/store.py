"""短事务有界消费成功审查，原work锁串行化唯一事实。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import exists, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.execution.release.observer import utc_now
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow
from opsswarm.investigation.reinvestigation_dispatch.source import event_cursor
from opsswarm.investigation.reinvestigation_review.tables import ReflowReviewJobRow
from opsswarm.investigation.reinvestigation_routing.policy import compile_route
from opsswarm.investigation.reinvestigation_routing.source import current_basis, load_source
from opsswarm.investigation.reinvestigation_routing.tables import ReflowRouteRow
from opsswarm.investigation.workflow_versions import LIVE_DYNAMIC_INVESTIGATION_GRAPH_VERSION


class ReflowRoutingStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ):
        self._factory, self._clock = factory, clock

    async def record_next(self) -> bool:
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                run_id = await session.scalar(
                    select(ReflowReviewJobRow.investigation_run_id)
                    .where(
                        ReflowReviewJobRow.status == "succeeded",
                        ReflowReviewJobRow.id.not_in(select(ReflowRouteRow.investigation_run_id)),
                        ReflowReviewJobRow.id.not_in(skipped),
                        ~exists(
                            select(CallReservationRow.id).where(
                                CallReservationRow.investigation_run_id == ReflowReviewJobRow.id,
                                CallReservationRow.status == "reserved",
                            )
                        ),
                    )
                    .order_by(ReflowReviewJobRow.completed_at, ReflowReviewJobRow.id)
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
                if work is None or await session.get(ReflowRouteRow, run_id) is not None:
                    continue
                await session.scalar(
                    select(ReflowReviewJobRow)
                    .where(ReflowReviewJobRow.id == run_id)
                    .with_for_update()
                )
                await session.execute(text("SELECT lock_reflow_review_basis(:id)"), {"id": run_id})
                run = await session.get(InvestigationRunRow, run_id)
                assert run is not None
                source, review = await load_source(session, run, clock=self._clock)
                now = normalized_now(self._clock)
                budget = await read_budget_snapshot(session, run)
                if any(c.status == "reserved" for c in budget.calls):
                    continue
                content = compile_route(
                    source,
                    review,
                    budget,
                    now=now,
                    basis_event_cursor=await event_cursor(session, run),
                    basis_blocker=await current_basis(session, run, now),
                    manual_takeover=run.graph_version == LIVE_DYNAMIC_INVESTIGATION_GRAPH_VERSION,
                )
                session.add(
                    ReflowRouteRow(
                        investigation_run_id=run.id,
                        route=content.route,
                        content=content.model_dump(mode="json"),
                        content_sha256=content_hash(content),
                        created_at=now,
                    )
                )
                await session.flush()
                return True
        return False
