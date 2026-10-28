"""从132成功闭环读取权威来源，并区分事件前缀与当前资格。"""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.incidents.persistence.tables import IncidentEventRow, IncidentRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation_coordination.contracts import (
    ReflowError,
    ReflowResult,
    ReflowSource,
)
from opsswarm.investigation.reinvestigation_coordination.queries import read_job
from opsswarm.investigation.reinvestigation_coordination.source import check_current_basis
from opsswarm.investigation.reinvestigation_coordination.tables import ReflowJobRow, ReflowResultRow


async def load_source(
    session: AsyncSession,
    run: InvestigationRunRow,
    job: ReflowJobRow,
    *,
    clock: Callable[[], datetime],
) -> tuple[ReflowSource, ReflowResult, str, str]:
    view = await read_job(session, run, job, clock=clock)
    row = await session.get(ReflowResultRow, run.id)
    incident = await session.get(IncidentRow, run.incident_id)
    if view.status != "succeeded" or view.result is None or row is None or incident is None:
        raise ReflowError("dispatch_source_not_ready", "分派需要同根已完成的回流协调结果")
    mutation = await session.scalar(
        select(IncidentEventRow)
        .where(
            IncidentEventRow.incident_id == run.incident_id,
            IncidentEventRow.event_type.in_(("incident.created", "incident.status_changed")),
        )
        .order_by(IncidentEventRow.id.desc())
        .limit(1)
    )
    if mutation is None or (incident.status, incident.version) != (
        mutation.to_status,
        mutation.incident_version,
    ):
        raise ReflowError("dispatch_integrity_violation", "当前事故字段与持久状态事件不一致")
    return (
        ReflowSource.model_validate(job.source),
        view.result,
        row.content_sha256,
        incident.service_name,
    )


async def event_cursor(session: AsyncSession, run: InvestigationRunRow) -> int:
    value = await session.scalar(
        select(func.max(IncidentEventRow.id)).where(IncidentEventRow.incident_id == run.incident_id)
    )
    if value is None:
        raise ReflowError("dispatch_integrity_violation", "回流分派缺少事故事件")
    return value


async def historical_basis(
    session: AsyncSession, run: InvestigationRunRow, source: ReflowSource, cursor: int
) -> str | None:
    event = await session.get(IncidentEventRow, cursor)
    if event is None or event.incident_id != run.incident_id:
        raise ReflowError("dispatch_integrity_violation", "分派事件游标不属于原事故")
    mutation = await session.scalar(
        select(IncidentEventRow)
        .where(
            IncidentEventRow.incident_id == run.incident_id,
            IncidentEventRow.id <= cursor,
            IncidentEventRow.event_type.in_(("incident.created", "incident.status_changed")),
        )
        .order_by(IncidentEventRow.id.desc())
        .limit(1)
    )
    anchor = await session.scalar(
        select(IncidentEventRow.id).where(
            IncidentEventRow.incident_id == run.incident_id,
            IncidentEventRow.event_type == "incident.status_changed",
            IncidentEventRow.incident_version == source.incident_version,
        )
    )
    if mutation is None or anchor is None or cursor < anchor:
        raise ReflowError("dispatch_integrity_violation", "分派游标早于原失败收敛或缺少状态事实")
    alert = await session.scalar(
        select(IncidentEventRow.id)
        .where(
            IncidentEventRow.incident_id == run.incident_id,
            IncidentEventRow.id > anchor,
            IncidentEventRow.id <= cursor,
            IncidentEventRow.event_type == "incident.alert_attached",
        )
        .limit(1)
    )
    if (
        mutation.to_status != "investigating"
        or mutation.incident_version != source.incident_version
        or alert is not None
    ):
        return "reflow_source_changed"
    return None


async def current_basis(
    session: AsyncSession, run: InvestigationRunRow, source: ReflowSource, now: datetime
) -> str | None:
    try:
        await check_current_basis(session, run, source, now=now)
    except (ReflowError, CallBudgetExceeded) as error:
        if error.code not in ("reflow_source_changed", "run_deadline_exceeded"):
            raise
        return error.code
    return None
