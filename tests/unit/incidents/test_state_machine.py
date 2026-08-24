from datetime import UTC, datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from opsswarm.incidents import (
    ActorType,
    Incident,
    IncidentAction,
    IncidentActor,
    IncidentEventType,
    IncidentSeverity,
    IncidentStatus,
    IncidentValidationError,
    IncidentVersionConflict,
    InvalidIncidentTransition,
    create_incident,
    transition_incident,
)

INCIDENT_ID = UUID("9dbf0ad7-b2bf-4d2b-8a43-b3c468a37515")
BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
SYSTEM_ACTOR = IncidentActor(actor_type=ActorType.SYSTEM)
HUMAN_ACTOR = IncidentActor(actor_type=ActorType.HUMAN, actor_id="engineer-001")


def make_incident(status: IncidentStatus, *, version: int = 4) -> Incident:
    resolved_at = BASE_TIME if status in {IncidentStatus.RESOLVED, IncidentStatus.CLOSED} else None
    closed_at = BASE_TIME if status is IncidentStatus.CLOSED else None
    return Incident(
        id=uuid4(),
        service_name="order-service",
        title="订单接口大量返回 500",
        severity=IncidentSeverity.P1,
        status=status,
        fingerprint="order-service:http-500",
        version=version,
        started_at=BASE_TIME - timedelta(minutes=5),
        resolved_at=resolved_at,
        closed_at=closed_at,
        created_at=BASE_TIME - timedelta(minutes=5),
        updated_at=BASE_TIME,
    )


def test_create_incident_returns_open_snapshot_and_audit_event() -> None:
    china_time = datetime(2026, 1, 1, 16, 0, tzinfo=timezone(timedelta(hours=8)))

    mutation = create_incident(
        incident_id=INCIDENT_ID,
        service_name="order-service",
        title="订单接口大量返回 500",
        severity=IncidentSeverity.P1,
        fingerprint="order-service:http-500",
        started_at=china_time - timedelta(minutes=1),
        occurred_at=china_time,
        actor=SYSTEM_ACTOR,
        reason="接收到首条告警",
    )

    assert mutation.incident.id == INCIDENT_ID
    assert mutation.incident.status is IncidentStatus.OPEN
    assert mutation.incident.version == 1
    assert mutation.incident.created_at == BASE_TIME
    assert mutation.event.event_type is IncidentEventType.CREATED
    assert mutation.event.from_status is None
    assert mutation.event.to_status is IncidentStatus.OPEN
    assert mutation.event.incident_version == 1
    assert mutation.event.actor == SYSTEM_ACTOR
    assert mutation.event.reason == "接收到首条告警"


@pytest.mark.parametrize(
    ("source", "action", "target"),
    [
        (
            IncidentStatus.OPEN,
            IncidentAction.START_INVESTIGATION,
            IncidentStatus.INVESTIGATING,
        ),
        (
            IncidentStatus.INVESTIGATING,
            IncidentAction.REQUEST_APPROVAL,
            IncidentStatus.AWAITING_APPROVAL,
        ),
        (
            IncidentStatus.INVESTIGATING,
            IncidentAction.START_VERIFICATION,
            IncidentStatus.VERIFYING,
        ),
        (
            IncidentStatus.AWAITING_APPROVAL,
            IncidentAction.APPROVE_PLAN,
            IncidentStatus.MITIGATING,
        ),
        (
            IncidentStatus.AWAITING_APPROVAL,
            IncidentAction.REJECT_PLAN,
            IncidentStatus.INVESTIGATING,
        ),
        (
            IncidentStatus.MITIGATING,
            IncidentAction.COMPLETE_MITIGATION,
            IncidentStatus.VERIFYING,
        ),
        (
            IncidentStatus.MITIGATING,
            IncidentAction.FAIL_MITIGATION,
            IncidentStatus.INVESTIGATING,
        ),
        (
            IncidentStatus.VERIFYING,
            IncidentAction.CONFIRM_RECOVERY,
            IncidentStatus.RESOLVED,
        ),
        (
            IncidentStatus.VERIFYING,
            IncidentAction.FAIL_VERIFICATION,
            IncidentStatus.INVESTIGATING,
        ),
        (IncidentStatus.RESOLVED, IncidentAction.CLOSE, IncidentStatus.CLOSED),
        (IncidentStatus.RESOLVED, IncidentAction.REOPEN, IncidentStatus.INVESTIGATING),
    ],
)
def test_all_allowed_transitions(
    source: IncidentStatus,
    action: IncidentAction,
    target: IncidentStatus,
) -> None:
    incident = make_incident(source)
    occurred_at = BASE_TIME + timedelta(minutes=1)

    mutation = transition_incident(
        incident,
        action=action,
        expected_version=incident.version,
        occurred_at=occurred_at,
        actor=HUMAN_ACTOR,
        reason="执行领域动作",
    )

    assert mutation.incident.status is target
    assert mutation.incident.version == incident.version + 1
    assert mutation.incident.updated_at == occurred_at
    assert mutation.event.event_type is IncidentEventType.STATUS_CHANGED
    assert mutation.event.from_status is source
    assert mutation.event.to_status is target
    assert mutation.event.incident_version == mutation.incident.version
    assert mutation.event.actor == HUMAN_ACTOR


def test_transition_rejects_stale_version_before_checking_action() -> None:
    incident = make_incident(IncidentStatus.OPEN)

    with pytest.raises(IncidentVersionConflict) as captured:
        transition_incident(
            incident,
            action=IncidentAction.CLOSE,
            expected_version=incident.version - 1,
            occurred_at=BASE_TIME,
            actor=HUMAN_ACTOR,
            reason="使用了过期页面",
        )

    assert captured.value.expected_version == incident.version - 1
    assert captured.value.actual_version == incident.version


def test_transition_rejects_action_not_allowed_in_current_status() -> None:
    incident = make_incident(IncidentStatus.OPEN)

    with pytest.raises(InvalidIncidentTransition) as captured:
        transition_incident(
            incident,
            action=IncidentAction.CLOSE,
            expected_version=incident.version,
            occurred_at=BASE_TIME,
            actor=HUMAN_ACTOR,
            reason="尝试提前关闭",
        )

    assert captured.value.status == IncidentStatus.OPEN.value
    assert captured.value.action == IncidentAction.CLOSE.value


def test_transition_rejects_time_before_current_snapshot() -> None:
    incident = make_incident(IncidentStatus.OPEN)

    with pytest.raises(IncidentValidationError, match="不能早于"):
        transition_incident(
            incident,
            action=IncidentAction.START_INVESTIGATION,
            expected_version=incident.version,
            occurred_at=BASE_TIME - timedelta(seconds=1),
            actor=HUMAN_ACTOR,
            reason="开始调查",
        )


def test_recovery_close_and_reopen_manage_lifecycle_times() -> None:
    verifying = make_incident(IncidentStatus.VERIFYING)
    resolved_time = BASE_TIME + timedelta(minutes=1)
    resolved = transition_incident(
        verifying,
        action=IncidentAction.CONFIRM_RECOVERY,
        expected_version=verifying.version,
        occurred_at=resolved_time,
        actor=HUMAN_ACTOR,
        reason="恢复指标持续达标",
    ).incident

    assert resolved.resolved_at == resolved_time
    assert resolved.closed_at is None

    closed_time = resolved_time + timedelta(minutes=1)
    closed = transition_incident(
        resolved,
        action=IncidentAction.CLOSE,
        expected_version=resolved.version,
        occurred_at=closed_time,
        actor=HUMAN_ACTOR,
        reason="工程师确认关闭",
    ).incident

    assert closed.resolved_at == resolved_time
    assert closed.closed_at == closed_time

    reopened = transition_incident(
        resolved,
        action=IncidentAction.REOPEN,
        expected_version=resolved.version,
        occurred_at=closed_time,
        actor=SYSTEM_ACTOR,
        reason="关闭前再次收到同类告警",
    ).incident

    assert reopened.status is IncidentStatus.INVESTIGATING
    assert reopened.resolved_at is None
    assert reopened.closed_at is None


@pytest.mark.parametrize("action", list(IncidentAction))
def test_closed_incident_rejects_every_action(action: IncidentAction) -> None:
    incident = make_incident(IncidentStatus.CLOSED)

    with pytest.raises(InvalidIncidentTransition):
        transition_incident(
            incident,
            action=action,
            expected_version=incident.version,
            occurred_at=BASE_TIME,
            actor=HUMAN_ACTOR,
            reason="closed 是终态",
        )
