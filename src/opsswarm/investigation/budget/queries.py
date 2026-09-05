"""从有界预留账本读取预算状态，不推断供应商用量。"""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.contracts import (
    CallBudgetPolicy,
    CallBudgetSnapshot,
    CallReservationRecord,
)
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.errors import InvestigationRunNotFound
from opsswarm.investigation.persistence.tables import InvestigationRunRow


class CallBudgetQueries:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def get_snapshot(self, run_id: UUID) -> CallBudgetSnapshot:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, run_id)
            if run is None:
                raise InvestigationRunNotFound(run_id)
            policy = (
                CallBudgetPolicy.model_validate(run.call_budget)
                if run.call_budget is not None
                else None
            )
            rows = list(
                await session.scalars(
                    select(CallReservationRow)
                    .where(
                        CallReservationRow.investigation_run_id == run_id,
                    )
                    .order_by(CallReservationRow.sequence)
                )
            )
            return CallBudgetSnapshot(
                investigation_run_id=run_id,
                policy=policy,
                started_at=run.started_at,
                deadline=run.started_at + timedelta(seconds=policy.max_duration_seconds)
                if policy and run.started_at
                else None,
                reserved_model_calls=sum(r.kind == "model" for r in rows) if policy else None,
                reserved_tool_calls=sum(r.kind == "tool" for r in rows) if policy else None,
                calls=tuple(
                    CallReservationRecord.model_validate(r, from_attributes=True) for r in rows
                ),
            )
