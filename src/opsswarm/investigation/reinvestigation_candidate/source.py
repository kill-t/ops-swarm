"""同事务原生重建完整专业闭包，当前资格与历史事实分别核验。"""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation.tables import CandidateReinvestigationRow
from opsswarm.investigation.reinvestigation_candidate.context import build_packet
from opsswarm.investigation.reinvestigation_candidate.contracts import (
    ReflowCandidateError,
    ReflowCandidatePacket,
    ReflowCandidateSource,
)
from opsswarm.investigation.reinvestigation_candidate.tables import ReflowCandidateJobRow


async def rebuild_source(
    session: AsyncSession,
    run: InvestigationRunRow,
    admission: CandidateReinvestigationRow,
    *,
    created_at: datetime,
    clock: Callable[[], datetime],
    require_applicable: bool = False,
) -> ReflowCandidateSource:
    value = await session.scalar(
        text("SELECT reflow_candidate_source(:id,:at)"), {"id": run.id, "at": created_at}
    )
    if value is None:
        raise ReflowCandidateError(
            "reflow_candidate_routing_incomplete", "必须等待候选评估路由及完整成功审查"
        )
    source = ReflowCandidateSource.model_validate(value)
    if require_applicable:
        await require_basis(session, run, source, clock=clock)
    return source


async def require_basis(
    session: AsyncSession,
    run: InvestigationRunRow,
    source: ReflowCandidateSource,
    *,
    clock: Callable[[], datetime],
) -> None:
    from opsswarm.investigation.reinvestigation_routing.source import current_basis

    blocker = await current_basis(session, run, clock())
    if blocker:
        raise ReflowCandidateError(blocker, "候选来源变化或原总期限已过")


async def load_source(
    session: AsyncSession,
    run: InvestigationRunRow,
    admission: CandidateReinvestigationRow,
    *,
    created_at: datetime,
    clock: Callable[[], datetime],
) -> ReflowCandidateSource:
    return await rebuild_source(
        session, run, admission, created_at=created_at, clock=clock, require_applicable=True
    )


async def require_current(
    session: AsyncSession,
    run: InvestigationRunRow,
    job: ReflowCandidateJobRow,
    *,
    clock: Callable[[], datetime],
) -> tuple[ReflowCandidateSource, ReflowCandidatePacket]:
    admission = await session.get(CandidateReinvestigationRow, run.id)
    if admission is None:
        raise ReflowCandidateError("reflow_candidate_source_invalid", "候选缺少原准入")
    source = await rebuild_source(
        session, run, admission, created_at=job.created_at, clock=clock, require_applicable=True
    )
    packet = build_packet(source)
    if (
        source.model_dump(mode="json") != job.source
        or content_hash(source) != job.source_sha256
        or packet.model_dump(mode="json") != job.packet
        or content_hash(packet) != job.packet_sha256
    ):
        raise ReflowCandidateError("reflow_candidate_source_invalid", "候选冻结来源或输入漂移")
    return source, packet
