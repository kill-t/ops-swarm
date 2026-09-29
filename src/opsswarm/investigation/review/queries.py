"""独立审查只读查询与来源复核。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.errors import InvestigationRunNotFound
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review.contracts import HypothesisReviewRecord
from opsswarm.investigation.review.persistence import validated_hypothesis_review
from opsswarm.investigation.review.tables import InvestigationHypothesisReviewRow
from opsswarm.investigation.tasks.tables import InvestigationRoundRow


class HypothesisReviewQueries:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_for_run(self, run_id: UUID) -> tuple[HypothesisReviewRecord, ...]:
        async with self._session_factory() as session:
            if await session.get(InvestigationRunRow, run_id) is None:
                raise InvestigationRunNotFound(run_id)
            rows = list(
                await session.scalars(
                    select(InvestigationHypothesisReviewRow)
                    .join(
                        InvestigationRoundRow,
                        InvestigationRoundRow.id == InvestigationHypothesisReviewRow.round_id,
                    )
                    .where(InvestigationHypothesisReviewRow.investigation_run_id == run_id)
                    .order_by(
                        InvestigationRoundRow.round_number,
                        InvestigationHypothesisReviewRow.id,
                    )
                )
            )
            records = []
            for row in rows:
                records.append(await validated_hypothesis_review(session, row))
            return tuple(records)
