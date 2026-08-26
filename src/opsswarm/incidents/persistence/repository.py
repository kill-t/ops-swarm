"""Incident 聚合的 PostgreSQL 仓储与事务边界。"""

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy import select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents.alerts import AlertIngestionResult, AlertSubmission
from opsswarm.incidents.errors import IncidentNotFound, IncidentVersionConflict
from opsswarm.incidents.models import (
    ActorType,
    Incident,
    IncidentAction,
    IncidentActor,
    IncidentEvent,
    IncidentEventType,
    IncidentSeverity,
    IncidentStatus,
)
from opsswarm.incidents.persistence.records import StoredIncident, StoredIncidentEvent
from opsswarm.incidents.persistence.tables import AlertRow, IncidentEventRow, IncidentRow
from opsswarm.incidents.state_machine import (
    create_incident,
    record_alert_attached,
    transition_incident,
)

_RETRYABLE_CONSTRAINTS = {
    "uq_alerts_source_external_event_id",
    "uq_incidents_active_fingerprint",
}
_MAX_INGESTION_ATTEMPTS = 3


class IncidentStore:
    """把领域行为和 PostgreSQL 原子事务连接起来。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def ingest_alert(self, submission: AlertSubmission) -> AlertIngestionResult:
        """幂等接收告警，并按指纹创建或聚合到未关闭 Incident。"""

        for attempt in range(_MAX_INGESTION_ATTEMPTS):
            try:
                return await self._ingest_alert_once(submission)
            except IntegrityError as error:
                constraint_name = _constraint_name(error)
                is_last_attempt = attempt == _MAX_INGESTION_ATTEMPTS - 1
                if constraint_name not in _RETRYABLE_CONSTRAINTS or is_last_attempt:
                    raise

        raise RuntimeError("告警接收重试循环意外结束")

    async def get_incident(self, incident_id: UUID) -> StoredIncident:
        """读取一个 Incident 当前快照。"""

        async with self._session_factory() as session:
            row = await session.get(IncidentRow, incident_id)
            if row is None:
                raise IncidentNotFound(str(incident_id))
            return _stored_incident(row)

    async def list_incidents(
        self,
        *,
        status: IncidentStatus | None = None,
        before_display_number: int | None = None,
        limit: int = 50,
    ) -> Sequence[StoredIncident]:
        """按展示序号倒序读取稳定的 Incident 游标分页。"""

        if before_display_number is not None and before_display_number < 1:
            raise ValueError("before_display_number 必须大于等于 1")
        if not 1 <= limit <= 500:
            raise ValueError("limit 必须在 1 到 500 之间")

        statement = select(IncidentRow)
        if status is not None:
            statement = statement.where(IncidentRow.status == status.value)
        if before_display_number is not None:
            statement = statement.where(IncidentRow.display_number < before_display_number)
        statement = statement.order_by(IncidentRow.display_number.desc()).limit(limit)

        async with self._session_factory() as session:
            rows = (await session.scalars(statement)).all()
            return tuple(_stored_incident(row) for row in rows)

    async def transition(
        self,
        incident_id: UUID,
        *,
        action: IncidentAction,
        expected_version: int,
        occurred_at: datetime,
        actor: IncidentActor,
        reason: str,
    ) -> StoredIncident:
        """原子更新乐观锁版本并追加对应状态事件。"""

        async with self._session_factory() as session, session.begin():
            return await transition_incident_in_session(
                session,
                incident_id,
                action=action,
                expected_version=expected_version,
                occurred_at=occurred_at,
                actor=actor,
                reason=reason,
            )

    async def list_events(
        self,
        incident_id: UUID,
        *,
        after_cursor: int = 0,
        limit: int = 100,
    ) -> Sequence[StoredIncidentEvent]:
        """按全局游标读取时间线，为后续 SSE 重放提供语义。"""

        if after_cursor < 0:
            raise ValueError("after_cursor 不能小于 0")
        if not 1 <= limit <= 500:
            raise ValueError("limit 必须在 1 到 500 之间")

        async with self._session_factory() as session:
            if await session.get(IncidentRow, incident_id) is None:
                raise IncidentNotFound(str(incident_id))
            rows = (
                await session.scalars(
                    select(IncidentEventRow)
                    .where(
                        IncidentEventRow.incident_id == incident_id,
                        IncidentEventRow.id > after_cursor,
                    )
                    .order_by(IncidentEventRow.id)
                    .limit(limit)
                )
            ).all()
            return tuple(_stored_event(row) for row in rows)

    async def _ingest_alert_once(self, submission: AlertSubmission) -> AlertIngestionResult:
        async with self._session_factory() as session, session.begin():
            existing_alert = await session.scalar(
                select(AlertRow).where(
                    AlertRow.source == submission.source,
                    AlertRow.external_event_id == submission.external_event_id,
                )
            )
            if existing_alert is not None:
                incident_row = await session.get(IncidentRow, existing_alert.incident_id)
                if incident_row is None:
                    raise IncidentNotFound(str(existing_alert.incident_id))
                return AlertIngestionResult(
                    incident=_incident_from_row(incident_row),
                    display_number=incident_row.display_number,
                    alert_id=existing_alert.id,
                    deduplicated=True,
                    created_incident=False,
                )

            incident_row = await session.scalar(
                select(IncidentRow)
                .where(
                    IncidentRow.fingerprint == submission.fingerprint,
                    IncidentRow.status != IncidentStatus.CLOSED.value,
                )
                .limit(1)
                .with_for_update()
            )
            alert_id = uuid4()
            system_actor = IncidentActor(actor_type=ActorType.SYSTEM)

            if incident_row is not None:
                incident = _incident_from_row(incident_row)
                alert_event = record_alert_attached(
                    incident,
                    occurred_at=submission.ingested_at,
                    actor=system_actor,
                    reason="接收到同一指纹的新告警",
                )
                session.add_all(
                    [
                        _alert_row(alert_id, incident.id, submission),
                        _event_row(
                            alert_event,
                            payload=_alert_event_payload(alert_id, submission),
                        ),
                    ]
                )
                await session.flush()
                if incident.status is IncidentStatus.RESOLVED:
                    reopened = await transition_incident_in_session(
                        session,
                        incident.id,
                        action=IncidentAction.REOPEN,
                        expected_version=incident.version,
                        occurred_at=submission.ingested_at,
                        actor=system_actor,
                        reason="恢复后同一指纹告警触发重新调查",
                    )
                    incident = reopened.incident
                return AlertIngestionResult(
                    incident=incident,
                    display_number=incident_row.display_number,
                    alert_id=alert_id,
                    deduplicated=False,
                    created_incident=False,
                )

            mutation = create_incident(
                incident_id=uuid4(),
                service_name=submission.service_name,
                title=submission.title,
                severity=submission.severity,
                fingerprint=submission.fingerprint,
                started_at=submission.occurred_at,
                occurred_at=submission.ingested_at,
                actor=system_actor,
                reason="接收到首条告警",
            )
            incident_row = _incident_row(mutation.incident)
            session.add(incident_row)
            await session.flush()
            session.add_all(
                [
                    _alert_row(alert_id, mutation.incident.id, submission),
                    _event_row(
                        mutation.event,
                        payload=_alert_event_payload(alert_id, submission),
                    ),
                ]
            )
            await session.flush()
            return AlertIngestionResult(
                incident=mutation.incident,
                display_number=incident_row.display_number,
                alert_id=alert_id,
                deduplicated=False,
                created_incident=True,
            )


def _incident_row(incident: Incident) -> IncidentRow:
    return IncidentRow(
        id=incident.id,
        service_name=incident.service_name,
        title=incident.title,
        severity=incident.severity.value,
        status=incident.status.value,
        fingerprint=incident.fingerprint,
        version=incident.version,
        started_at=incident.started_at,
        resolved_at=incident.resolved_at,
        closed_at=incident.closed_at,
        created_at=incident.created_at,
        updated_at=incident.updated_at,
    )


def _incident_from_row(row: IncidentRow) -> Incident:
    return Incident(
        id=row.id,
        service_name=row.service_name,
        title=row.title,
        severity=IncidentSeverity(row.severity),
        status=IncidentStatus(row.status),
        fingerprint=row.fingerprint,
        version=row.version,
        started_at=row.started_at,
        resolved_at=row.resolved_at,
        closed_at=row.closed_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _stored_incident(row: IncidentRow) -> StoredIncident:
    return StoredIncident(
        incident=_incident_from_row(row),
        display_number=row.display_number,
    )


def _alert_row(alert_id: UUID, incident_id: UUID, submission: AlertSubmission) -> AlertRow:
    return AlertRow(
        id=alert_id,
        incident_id=incident_id,
        source=submission.source,
        external_event_id=submission.external_event_id,
        fingerprint=submission.fingerprint,
        service_name=submission.service_name,
        severity=submission.severity.value,
        title=submission.title,
        payload=dict(submission.payload),
        occurred_at=submission.occurred_at,
        ingested_at=submission.ingested_at,
    )


def _event_row(
    event: IncidentEvent,
    *,
    payload: Mapping[str, Any],
) -> IncidentEventRow:
    return IncidentEventRow(
        incident_id=event.incident_id,
        event_type=event.event_type.value,
        actor_type=event.actor.actor_type.value,
        actor_id=event.actor.actor_id,
        from_status=event.from_status.value if event.from_status is not None else None,
        to_status=event.to_status.value,
        reason=event.reason,
        incident_version=event.incident_version,
        payload=dict(payload),
        occurred_at=event.occurred_at,
    )


def _stored_event(row: IncidentEventRow) -> StoredIncidentEvent:
    event = IncidentEvent(
        event_type=IncidentEventType(row.event_type),
        incident_id=row.incident_id,
        actor=IncidentActor(
            actor_type=ActorType(row.actor_type),
            actor_id=row.actor_id,
        ),
        from_status=IncidentStatus(row.from_status) if row.from_status is not None else None,
        to_status=IncidentStatus(row.to_status),
        reason=row.reason,
        occurred_at=row.occurred_at,
        incident_version=row.incident_version,
    )
    return StoredIncidentEvent(cursor=row.id, event=event, payload=row.payload)


def _alert_event_payload(alert_id: UUID, submission: AlertSubmission) -> dict[str, str]:
    return {
        "alert_id": str(alert_id),
        "source": submission.source,
        "external_event_id": submission.external_event_id,
    }


def _constraint_name(error: IntegrityError) -> str | None:
    diagnostic = getattr(error.orig, "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    return constraint_name if isinstance(constraint_name, str) else None


async def transition_incident_in_session(
    session: AsyncSession,
    incident_id: UUID,
    *,
    action: IncidentAction,
    expected_version: int,
    occurred_at: datetime,
    actor: IncidentActor,
    reason: str,
) -> StoredIncident:
    """在调用方事务中迁移 Incident，供跨模块原子用例复用。"""

    row = await session.get(IncidentRow, incident_id)
    if row is None:
        raise IncidentNotFound(str(incident_id))

    mutation = transition_incident(
        _incident_from_row(row),
        action=action,
        expected_version=expected_version,
        occurred_at=occurred_at,
        actor=actor,
        reason=reason,
    )
    updated = mutation.incident
    result = cast(
        CursorResult[Any],
        await session.execute(
            update(IncidentRow)
            .where(
                IncidentRow.id == incident_id,
                IncidentRow.version == expected_version,
            )
            .values(
                status=updated.status.value,
                version=updated.version,
                resolved_at=updated.resolved_at,
                closed_at=updated.closed_at,
                updated_at=updated.updated_at,
            )
        ),
    )
    if result.rowcount != 1:
        actual_version = await session.scalar(
            select(IncidentRow.version).where(IncidentRow.id == incident_id)
        )
        if actual_version is None:
            raise IncidentNotFound(str(incident_id))
        raise IncidentVersionConflict(
            expected_version=expected_version,
            actual_version=actual_version,
        )

    session.add(
        _event_row(
            mutation.event,
            payload={"action": action.value},
        )
    )
    await session.flush()
    return StoredIncident(incident=updated, display_number=row.display_number)
