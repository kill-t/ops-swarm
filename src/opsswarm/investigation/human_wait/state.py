"""人工等待查询与后续准备共用的锁和完整来源端口。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.handoff.contracts import TerminalHandoffRecord
from opsswarm.investigation.handoff.persistence import validated_handoff
from opsswarm.investigation.handoff.tables import InvestigationTerminalHandoffRow
from opsswarm.investigation.human_wait.contracts import HumanWaitError, HumanWaitState
from opsswarm.investigation.human_wait.validation import project_wait_state, validated_wait_events
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow
from opsswarm.investigation.tasks.errors import TaskFactConflict


async def lock_wait_run(session: AsyncSession, run_id: UUID, *, read: bool) -> InvestigationRunRow:
    work = await session.scalar(
        select(WorkItemRow)
        .where(WorkItemRow.investigation_run_id == run_id)
        .with_for_update(read=read)
    )
    run = await session.get(InvestigationRunRow, run_id)
    if run is None:
        raise HumanWaitError("human_wait_run_not_found", "调查运行不存在", 404)
    if work is None:
        raise HumanWaitError("human_wait_integrity_violation", "调查运行缺少父工作项")
    return run


async def load_wait_state(
    session: AsyncSession, run_id: UUID
) -> tuple[TerminalHandoffRecord, HumanWaitState]:
    row = await session.scalar(
        select(InvestigationTerminalHandoffRow).where(
            InvestigationTerminalHandoffRow.investigation_run_id == run_id
        )
    )
    if row is None or row.destination != "await_human":
        raise HumanWaitError("human_wait_not_available", "运行未交接至人工等待")
    try:
        handoff = await validated_handoff(session, row)
    except TaskFactConflict as error:
        raise HumanWaitError(
            "human_wait_integrity_violation", "人工等待交接来源校验失败"
        ) from error
    events = await validated_wait_events(session, handoff)
    return handoff, project_wait_state(handoff, events)
