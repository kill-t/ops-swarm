"""重建精确成功审查；当前诊断独立于历史来源。"""

from collections.abc import Callable
from datetime import datetime
from typing import cast

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation_review.contracts import (
    ReflowReviewError,
    ReflowReviewResult,
    ReflowReviewSource,
)
from opsswarm.investigation.reinvestigation_review.queries import read_job
from opsswarm.investigation.reinvestigation_review.tables import ReflowReviewJobRow
from opsswarm.investigation.reinvestigation_routing.contracts import BasisBlocker


async def load_source(
    session: AsyncSession, run: InvestigationRunRow, *, clock: Callable[[], datetime]
) -> tuple[ReflowReviewSource, ReflowReviewResult]:
    job = await session.get(ReflowReviewJobRow, run.id)
    view = await read_job(session, run, job, clock=clock)
    if job is None or job.source is None or view.result is None or view.status != "succeeded":
        raise ReflowReviewError("reflow_routing_review_incomplete", "必须等待完整成功审查")
    # 原生函数另行核验完整真实调用者/发布尝试闭包，不能只接受摘要自洽。
    await session.execute(
        text("SELECT reflow_routing_source(:id,:at)"), {"id": run.id, "at": clock()}
    )
    return ReflowReviewSource.model_validate(job.source), view.result


async def current_basis(
    session: AsyncSession, run: InvestigationRunRow, now: datetime
) -> BasisBlocker | None:
    reason = await session.scalar(
        text("SELECT reflow_review_basis_blocker(:id,:at)"), {"id": run.id, "at": now}
    )
    if reason not in (None, "reflow_source_changed", "run_deadline_exceeded"):
        raise ReflowReviewError("reflow_routing_integrity_violation", "无法识别当前依据阻断")
    return cast(BasisBlocker | None, reason)


async def historical_basis(
    session: AsyncSession, run: InvestigationRunRow, cursor: int, now: datetime
) -> BasisBlocker | None:
    reason = await session.scalar(
        text("SELECT reflow_routing_historical_basis(:id,:cursor,:at)"),
        {"id": run.id, "cursor": cursor, "at": now},
    )
    return cast(BasisBlocker | None, reason)
