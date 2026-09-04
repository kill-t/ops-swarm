"""确定性生成并保存事故复盘报告。"""

import hashlib
import json
from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents import (
    ActorType,
    IncidentEventType,
    IncidentNotFound,
    IncidentSeverity,
    IncidentStatus,
    IncidentVersionConflict,
)
from opsswarm.incidents.persistence.tables import IncidentEventRow, IncidentRow
from opsswarm.reports.errors import (
    IncidentReportIntegrityViolation,
    IncidentReportNotFound,
    IncidentReportNotReady,
    IncidentReportTooLarge,
)
from opsswarm.reports.models import (
    REPORT_SCHEMA_VERSION,
    IncidentReportContent,
    IncidentReportCreation,
    IncidentReportIncident,
    IncidentReportTimelineEntry,
    StoredIncidentReport,
)
from opsswarm.reports.persistence.tables import IncidentReportRow
from opsswarm.workroom.repository import load_workroom_snapshot_in_session

MAX_REPORT_TIMELINE_EVENTS = 500


class IncidentReportStore:
    """以业务库事实冻结报告，重复请求只返回原快照。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create(
        self,
        incident_id: UUID,
        *,
        expected_incident_version: int,
        generated_by_id: str,
        occurred_at: datetime,
    ) -> IncidentReportCreation:
        """锁定已关闭 Incident，并在同一事务中冻结完整报告。"""

        async with self._session_factory() as session, session.begin():
            existing = await session.scalar(
                select(IncidentReportRow).where(IncidentReportRow.incident_id == incident_id)
            )
            if existing is not None:
                return IncidentReportCreation(created=False, report=_stored_report(existing))

            incident = await session.scalar(
                select(IncidentRow).where(IncidentRow.id == incident_id).with_for_update()
            )
            if incident is None:
                raise IncidentNotFound(str(incident_id))
            existing = await session.scalar(
                select(IncidentReportRow).where(IncidentReportRow.incident_id == incident_id)
            )
            if existing is not None:
                return IncidentReportCreation(created=False, report=_stored_report(existing))
            if incident.status != IncidentStatus.CLOSED.value:
                raise IncidentReportNotReady(incident_id, status=incident.status)
            if incident.version != expected_incident_version:
                raise IncidentVersionConflict(
                    expected_version=expected_incident_version,
                    actual_version=incident.version,
                )
            if incident.resolved_at is None or incident.closed_at is None:
                raise IncidentReportNotReady(incident_id, status=incident.status)

            workroom = await load_workroom_snapshot_in_session(session, incident_id)
            event_rows = tuple(
                await session.scalars(
                    select(IncidentEventRow)
                    .where(IncidentEventRow.incident_id == incident_id)
                    .order_by(IncidentEventRow.id)
                    .limit(MAX_REPORT_TIMELINE_EVENTS + 1)
                )
            )
            if len(event_rows) > MAX_REPORT_TIMELINE_EVENTS:
                raise IncidentReportTooLarge(
                    incident_id,
                    maximum=MAX_REPORT_TIMELINE_EVENTS,
                )

            content = IncidentReportContent(
                generated_at=occurred_at,
                generated_by_id=generated_by_id,
                incident=IncidentReportIncident(
                    id=incident.id,
                    display_number=incident.display_number,
                    reference=f"INC-{incident.display_number:06d}",
                    service_name=incident.service_name,
                    title=incident.title,
                    severity=IncidentSeverity(incident.severity),
                    status=IncidentStatus.CLOSED,
                    fingerprint=incident.fingerprint,
                    version=incident.version,
                    started_at=incident.started_at,
                    resolved_at=incident.resolved_at,
                    closed_at=incident.closed_at,
                    duration_seconds=int(
                        (incident.resolved_at - incident.started_at).total_seconds()
                    ),
                ),
                workroom=workroom,
                timeline=tuple(_timeline_entry(row) for row in event_rows),
            )
            content_json = content.model_dump(mode="json")
            row = IncidentReportRow(
                id=uuid4(),
                incident_id=incident.id,
                incident_version=incident.version,
                schema_version=REPORT_SCHEMA_VERSION,
                content_hash=_content_hash(content),
                content=content_json,
                created_at=occurred_at,
            )
            session.add(row)
            await session.flush()
            return IncidentReportCreation(created=True, report=_stored_report(row))

    async def get(self, incident_id: UUID) -> StoredIncidentReport:
        """读取报告并重算摘要，防止静默返回被篡改的审计快照。"""

        async with self._session_factory() as session:
            row = await session.scalar(
                select(IncidentReportRow).where(IncidentReportRow.incident_id == incident_id)
            )
            if row is None:
                if await session.get(IncidentRow, incident_id) is None:
                    raise IncidentNotFound(str(incident_id))
                raise IncidentReportNotFound(incident_id)
            return _stored_report(row)


def _timeline_entry(row: IncidentEventRow) -> IncidentReportTimelineEntry:
    return IncidentReportTimelineEntry(
        cursor=row.id,
        event_type=IncidentEventType(row.event_type),
        actor_type=ActorType(row.actor_type),
        actor_id=row.actor_id,
        from_status=IncidentStatus(row.from_status) if row.from_status is not None else None,
        to_status=IncidentStatus(row.to_status),
        reason=row.reason,
        incident_version=row.incident_version,
        payload=row.payload,
        occurred_at=row.occurred_at,
    )


def _content_hash(content: IncidentReportContent) -> str:
    canonical_json = json.dumps(
        content.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _stored_report(row: IncidentReportRow) -> StoredIncidentReport:
    content = IncidentReportContent.model_validate(row.content)
    if row.schema_version != REPORT_SCHEMA_VERSION or _content_hash(content) != row.content_hash:
        raise IncidentReportIntegrityViolation(row.id)
    return StoredIncidentReport(
        id=row.id,
        incident_id=row.incident_id,
        incident_version=row.incident_version,
        schema_version=REPORT_SCHEMA_VERSION,
        content_hash=row.content_hash,
        content=content,
        created_at=row.created_at,
    )
