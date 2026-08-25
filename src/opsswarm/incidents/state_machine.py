"""Incident 纯领域状态机。"""

from dataclasses import replace
from datetime import datetime
from uuid import UUID

from opsswarm.incidents.errors import (
    IncidentValidationError,
    IncidentVersionConflict,
    InvalidIncidentTransition,
)
from opsswarm.incidents.models import (
    Incident,
    IncidentAction,
    IncidentActor,
    IncidentEvent,
    IncidentEventType,
    IncidentMutation,
    IncidentSeverity,
    IncidentStatus,
    normalize_utc,
)

_TRANSITIONS: dict[tuple[IncidentStatus, IncidentAction], IncidentStatus] = {
    (IncidentStatus.OPEN, IncidentAction.START_INVESTIGATION): IncidentStatus.INVESTIGATING,
    (
        IncidentStatus.INVESTIGATING,
        IncidentAction.REQUEST_APPROVAL,
    ): IncidentStatus.AWAITING_APPROVAL,
    (
        IncidentStatus.INVESTIGATING,
        IncidentAction.START_VERIFICATION,
    ): IncidentStatus.VERIFYING,
    (
        IncidentStatus.AWAITING_APPROVAL,
        IncidentAction.APPROVE_PLAN,
    ): IncidentStatus.MITIGATING,
    (
        IncidentStatus.AWAITING_APPROVAL,
        IncidentAction.REJECT_PLAN,
    ): IncidentStatus.INVESTIGATING,
    (
        IncidentStatus.AWAITING_APPROVAL,
        IncidentAction.EXPIRE_APPROVAL,
    ): IncidentStatus.INVESTIGATING,
    (
        IncidentStatus.MITIGATING,
        IncidentAction.COMPLETE_MITIGATION,
    ): IncidentStatus.VERIFYING,
    (
        IncidentStatus.MITIGATING,
        IncidentAction.FAIL_MITIGATION,
    ): IncidentStatus.INVESTIGATING,
    (
        IncidentStatus.VERIFYING,
        IncidentAction.CONFIRM_RECOVERY,
    ): IncidentStatus.RESOLVED,
    (
        IncidentStatus.VERIFYING,
        IncidentAction.FAIL_VERIFICATION,
    ): IncidentStatus.INVESTIGATING,
    (IncidentStatus.RESOLVED, IncidentAction.CLOSE): IncidentStatus.CLOSED,
    (IncidentStatus.RESOLVED, IncidentAction.REOPEN): IncidentStatus.INVESTIGATING,
}


def create_incident(
    *,
    incident_id: UUID,
    service_name: str,
    title: str,
    severity: IncidentSeverity,
    fingerprint: str,
    started_at: datetime,
    occurred_at: datetime,
    actor: IncidentActor,
    reason: str,
) -> IncidentMutation:
    """创建版本为 1 的 open Incident 及配套审计事件。"""

    normalized_time = normalize_utc(occurred_at, field_name="occurred_at")
    incident = Incident(
        id=incident_id,
        service_name=service_name,
        title=title,
        severity=severity,
        status=IncidentStatus.OPEN,
        fingerprint=fingerprint,
        version=1,
        started_at=started_at,
        resolved_at=None,
        closed_at=None,
        created_at=normalized_time,
        updated_at=normalized_time,
    )
    event = IncidentEvent(
        event_type=IncidentEventType.CREATED,
        incident_id=incident.id,
        actor=actor,
        from_status=None,
        to_status=IncidentStatus.OPEN,
        reason=reason,
        occurred_at=normalized_time,
        incident_version=incident.version,
    )
    return IncidentMutation(incident=incident, event=event)


def transition_incident(
    incident: Incident,
    *,
    action: IncidentAction,
    expected_version: int,
    occurred_at: datetime,
    actor: IncidentActor,
    reason: str,
) -> IncidentMutation:
    """校验动作与版本，返回需要原子持久化的新快照和事件。"""

    if not isinstance(incident, Incident):
        raise IncidentValidationError("incident 必须是 Incident")
    if not isinstance(action, IncidentAction):
        raise IncidentValidationError("action 必须是 IncidentAction")
    if (
        not isinstance(expected_version, int)
        or isinstance(expected_version, bool)
        or expected_version < 1
    ):
        raise IncidentValidationError("expected_version 必须是大于等于 1 的整数")
    if expected_version != incident.version:
        raise IncidentVersionConflict(
            expected_version=expected_version,
            actual_version=incident.version,
        )

    target_status = _TRANSITIONS.get((incident.status, action))
    if target_status is None:
        raise InvalidIncidentTransition(status=incident.status.value, action=action.value)

    normalized_time = normalize_utc(occurred_at, field_name="occurred_at")
    if normalized_time < incident.updated_at:
        raise IncidentValidationError("状态变更时间不能早于 Incident 当前更新时间")

    resolved_at = incident.resolved_at
    closed_at = incident.closed_at
    if target_status is IncidentStatus.RESOLVED:
        resolved_at = normalized_time
    elif target_status is IncidentStatus.INVESTIGATING:
        resolved_at = None
    if target_status is IncidentStatus.CLOSED:
        closed_at = normalized_time

    updated_incident = replace(
        incident,
        status=target_status,
        version=incident.version + 1,
        resolved_at=resolved_at,
        closed_at=closed_at,
        updated_at=normalized_time,
    )
    event = IncidentEvent(
        event_type=IncidentEventType.STATUS_CHANGED,
        incident_id=incident.id,
        actor=actor,
        from_status=incident.status,
        to_status=target_status,
        reason=reason,
        occurred_at=normalized_time,
        incident_version=updated_incident.version,
    )
    return IncidentMutation(incident=updated_incident, event=event)


def record_alert_attached(
    incident: Incident,
    *,
    occurred_at: datetime,
    actor: IncidentActor,
    reason: str,
) -> IncidentEvent:
    """为聚合到现有 Incident 的新告警产生时间线事件。"""

    if not isinstance(incident, Incident):
        raise IncidentValidationError("incident 必须是 Incident")
    return IncidentEvent(
        event_type=IncidentEventType.ALERT_ATTACHED,
        incident_id=incident.id,
        actor=actor,
        from_status=None,
        to_status=incident.status,
        reason=reason,
        occurred_at=occurred_at,
        incident_version=incident.version,
    )
