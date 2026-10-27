"""完整来源及持久动作事件的历史重建，不要求事故永远保持当时状态。"""

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.incidents.convergence.contracts import (
    ConvergenceContent,
    ConvergenceError,
    ConvergenceRecord,
)
from opsswarm.incidents.convergence.policy import compile_convergence
from opsswarm.incidents.convergence.tables import CandidateIncidentConvergenceRow
from opsswarm.incidents.models import ActorType, IncidentStatus
from opsswarm.incidents.persistence.tables import AlertRow, IncidentEventRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.settlement.queries import read_settlement_sources


async def read_convergence(
    session: AsyncSession,
    run: InvestigationRunRow,
    row: CandidateIncidentConvergenceRow,
    *,
    clock: Callable[[], datetime],
) -> ConvergenceRecord:
    try:
        content = ConvergenceContent.model_validate(row.content)
        settlement, recovery = await read_settlement_sources(session, run, clock=clock)
        alert = (
            None
            if content.latest_alert_id is None
            else await session.get(AlertRow, content.latest_alert_id)
        )
        if (content.latest_alert_id is not None and alert is None) or (
            alert is not None
            and (
                alert.incident_id != run.incident_id
                or alert.ingested_at != content.latest_alert_ingested_at
            )
        ):
            raise ValueError("收敛告警引用不一致")
        expected = compile_convergence(
            settlement,
            recovery,
            incident_id=run.incident_id,
            status=content.before_status,
            version=content.before_version,
            latest_alert_id=content.latest_alert_id,
            latest_alert_ingested_at=content.latest_alert_ingested_at,
            now=content.created_at,
        )
        if (
            row.investigation_run_id != run.id
            or row.incident_id != run.incident_id
            or content != expected
            or content.created_at != row.created_at
            or row.content_sha256 != content_hash(content)
            or row.content_sha256 != raw_content_hash(row.content)
            or run.candidate_slot_released_at
            != (content.created_at if content.slot_released else None)
        ):
            raise ValueError("收敛结果、摘要或槽位投影不一致")
        status = content.before_status
        event_count = await session.scalar(
            select(func.count())
            .select_from(IncidentEventRow)
            .where(
                IncidentEventRow.actor_id == f"candidate-convergence:{run.id}",
            )
        )
        if event_count != len(content.actions):
            raise ValueError("事故收敛事件数量与动作不一致")
        anchor = await session.scalar(
            select(IncidentEventRow).where(
                IncidentEventRow.incident_id == run.incident_id,
                IncidentEventRow.incident_version == content.before_version,
                IncidentEventRow.event_type.in_(("incident.created", "incident.status_changed")),
            )
        )
        if (
            anchor is None
            or anchor.to_status != status.value
            or anchor.occurred_at > content.created_at
        ):
            raise ValueError("事故收敛前态缺少精确历史版本事件")
        for index, action in enumerate(content.actions, 1):
            event = await session.scalar(
                select(IncidentEventRow).where(
                    IncidentEventRow.incident_id == run.incident_id,
                    IncidentEventRow.incident_version == content.before_version + index,
                    IncidentEventRow.event_type == "incident.status_changed",
                )
            )
            target = IncidentStatus.VERIFYING if index == 1 else content.after_status
            if (
                event is None
                or event.from_status != status.value
                or event.to_status != target.value
                or event.payload != {"action": action.value}
                or event.actor_type != ActorType.SYSTEM.value
                or event.actor_id != f"candidate-convergence:{run.id}"
                or event.reason != content.reason_code
                or event.occurred_at != content.created_at
            ):
                raise ValueError("事故收敛缺少精确动作事件")
            status = target
        return ConvergenceRecord(**content.model_dump(), content_sha256=row.content_sha256)
    except (ValueError, TypeError) as error:
        raise ConvergenceError(
            "candidate_convergence_integrity_violation", "事故收敛历史无法重建"
        ) from error
