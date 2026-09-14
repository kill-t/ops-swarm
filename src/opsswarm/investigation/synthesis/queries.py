"""开放假设只读查询与来源复核。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.errors import InvestigationRunNotFound
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.synthesis.contracts import OpenHypothesisRecord
from opsswarm.investigation.synthesis.persistence import validated_open_hypothesis
from opsswarm.investigation.synthesis.tables import InvestigationOpenHypothesisRow


class OpenHypothesisQueries:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def list_for_run(self, run_id: UUID) -> tuple[OpenHypothesisRecord, ...]:
        async with self._session_factory() as session:
            if await session.get(InvestigationRunRow, run_id) is None:
                raise InvestigationRunNotFound(run_id)
            rows = list(
                await session.scalars(
                    select(InvestigationOpenHypothesisRow)
                    .where(InvestigationOpenHypothesisRow.investigation_run_id == run_id)
                    .order_by(InvestigationOpenHypothesisRow.created_at)
                )
            )
            records = []
            for row in rows:
                records.append(await validated_open_hypothesis(session, row))
            return tuple(records)
