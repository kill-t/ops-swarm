"""同事务原生重建完整专业闭包，当前资格与历史事实分别核验。"""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation.tables import CandidateReinvestigationRow
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowSource
from opsswarm.investigation.reinvestigation_coordination.source import check_current_basis
from opsswarm.investigation.reinvestigation_review.context import build_packet
from opsswarm.investigation.reinvestigation_review.contracts import (
    ReflowReviewError,
    ReflowReviewPacket,
    ReflowReviewSource,
)
from opsswarm.investigation.reinvestigation_review.tables import ReflowReviewJobRow


async def rebuild_source(
    session: AsyncSession,
    run: InvestigationRunRow,
    admission: CandidateReinvestigationRow,
    *,
    created_at: datetime,
    clock: Callable[[], datetime],
    require_applicable: bool = False,
) -> ReflowReviewSource:
    value = await session.scalar(
        text("SELECT reflow_review_source(:id,:at)"), {"id": run.id, "at": created_at}
    )
    if value is None:
        raise ReflowReviewError("reflow_review_synthesis_incomplete", "必须等待完整成功综合结果")
    source = ReflowReviewSource.model_validate(value)
    if require_applicable:
        await require_basis(session, run, source, clock=clock)
    return source


async def require_basis(
    session: AsyncSession,
    run: InvestigationRunRow,
    source: ReflowReviewSource,
    *,
    clock: Callable[[], datetime],
) -> None:
    # 当前资格沿用132的事故与期限规则；SQL另行验证135完整历史闭包。
    original = ReflowSource.model_validate(
        {k: v for k, v in source.model_dump(mode="json").items() if k in ReflowSource.model_fields}
        | {
            "version": "reinvestigation-coordination-source-v1",
            "created_at": source.coordination_created_at,
        }
    )
    await check_current_basis(session, run, original, now=clock())


async def load_source(
    session: AsyncSession,
    run: InvestigationRunRow,
    admission: CandidateReinvestigationRow,
    *,
    created_at: datetime,
    clock: Callable[[], datetime],
) -> ReflowReviewSource:
    return await rebuild_source(
        session, run, admission, created_at=created_at, clock=clock, require_applicable=True
    )


async def require_current(
    session: AsyncSession,
    run: InvestigationRunRow,
    job: ReflowReviewJobRow,
    *,
    clock: Callable[[], datetime],
) -> tuple[ReflowReviewSource, ReflowReviewPacket]:
    admission = await session.get(CandidateReinvestigationRow, run.id)
    if admission is None:
        raise ReflowReviewError("reflow_review_source_invalid", "综合缺少原准入")
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
        raise ReflowReviewError("reflow_review_source_invalid", "综合冻结来源或输入漂移")
    return source, packet
