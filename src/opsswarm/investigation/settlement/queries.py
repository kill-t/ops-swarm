"""供后续事故收敛复用的完整收尾历史读取端口。"""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.execution.release.tables import CandidateReleaseRequestRow
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.settlement.contracts import SettlementError, SettlementRecord
from opsswarm.investigation.settlement.parent_state import parent_settlement
from opsswarm.verification.candidate.contracts import RecoveryView
from opsswarm.verification.candidate.queries import recovery_view
from opsswarm.verification.candidate.source import execution_source
from opsswarm.verification.candidate.tables import RecoveryJobRow


async def read_settlement(
    session: AsyncSession,
    run: InvestigationRunRow,
    *,
    clock: Callable[[], datetime],
) -> SettlementRecord:
    return (await read_settlement_sources(session, run, clock=clock))[0]


async def read_settlement_sources(
    session: AsyncSession,
    run: InvestigationRunRow,
    *,
    clock: Callable[[], datetime],
) -> tuple[SettlementRecord, RecoveryView]:
    record = await parent_settlement(session, run)
    row = await session.get(RecoveryJobRow, record.recovery_job_id)
    assert row is not None
    source = await execution_source(session, run, row.id, clock=clock)
    recovery = await recovery_view(session, row, run, source)
    assert source.result is not None
    request = await session.get(CandidateReleaseRequestRow, source.result.request_id)
    if request is None or request.incident_version != record.incident_version:
        raise SettlementError("candidate_settlement_integrity_violation", "收尾与原事故版本不一致")
    return record, recovery
