"""确定性审查路由的只读查询与来源复核。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.errors import InvestigationRunNotFound
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord
from opsswarm.investigation.review_routing.persistence import validated_review_routing
from opsswarm.investigation.review_routing.tables import InvestigationHypothesisReviewRouteRow


class ReviewRoutingQueries:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_for_run(self, run_id: UUID) -> tuple[ReviewRoutingRecord, ...]:
        async with self._session_factory() as session:
            if await session.get(InvestigationRunRow, run_id) is None:
                raise InvestigationRunNotFound(run_id)
            rows = list(
                await session.scalars(
                    select(InvestigationHypothesisReviewRouteRow)
                    .where(InvestigationHypothesisReviewRouteRow.investigation_run_id == run_id)
                    .order_by(
                        InvestigationHypothesisReviewRouteRow.created_at,
                        InvestigationHypothesisReviewRouteRow.id,
                    )
                )
            )
            records = []
            for row in rows:
                records.append(await validated_review_routing(session, row))
            return tuple(records)
