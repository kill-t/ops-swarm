"""从业务库查询并复核运行级协调策略与计划。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.coordination.contracts import (
    CoordinationPlanRecord,
    CoordinationPolicyRecord,
)
from opsswarm.investigation.coordination.persistence import validated_plan, validated_policy
from opsswarm.investigation.coordination.tables import (
    InvestigationCoordinationPlanRow,
    InvestigationCoordinationPolicyRow,
)
from opsswarm.investigation.errors import InvestigationRunNotFound
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.tables import InvestigationRoundRow


class CoordinationPlanQueries:
    """只读入口不消费租约，并在返回前重建持久来源。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def get_policy(self, run_id: UUID) -> CoordinationPolicyRecord | None:
        async with self._session_factory() as session:
            await _require_run(session, run_id)
            row = await session.get(InvestigationCoordinationPolicyRow, run_id)
            return validated_policy(row) if row is not None else None

    async def list_plans(self, run_id: UUID) -> tuple[CoordinationPlanRecord, ...]:
        async with self._session_factory() as session:
            await _require_run(session, run_id)
            rows = list(
                await session.scalars(
                    select(InvestigationCoordinationPlanRow)
                    .join(
                        InvestigationRoundRow,
                        InvestigationRoundRow.id == InvestigationCoordinationPlanRow.round_id,
                    )
                    .where(InvestigationCoordinationPlanRow.investigation_run_id == run_id)
                    .order_by(InvestigationRoundRow.round_number)
                )
            )
            records = []
            for row in rows:
                records.append(await validated_plan(session, row))
            return tuple(records)


async def _require_run(session: AsyncSession, run_id: UUID) -> None:
    if await session.get(InvestigationRunRow, run_id) is None:
        raise InvestigationRunNotFound(run_id)
