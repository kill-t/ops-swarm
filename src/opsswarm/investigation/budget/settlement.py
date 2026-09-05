"""在父尝试结束事务中收敛失联调用，不返还未知占用。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.budget.tables import CallReservationRow


async def close_open_reservations(session: AsyncSession, run_id: UUID, now: datetime) -> None:
    await session.execute(
        update(CallReservationRow)
        .where(
            CallReservationRow.investigation_run_id == run_id,
            CallReservationRow.status == "reserved",
        )
        .values(status="unknown", error_code="parent_attempt_ended", finished_at=now)
    )
