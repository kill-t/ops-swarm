from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest

from opsswarm.incidents import (
    ActorType,
    Incident,
    IncidentActor,
    IncidentSeverity,
    IncidentStatus,
    IncidentValidationError,
)

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def make_incident(**overrides: object) -> Incident:
    values: dict[str, object] = {
        "id": uuid4(),
        "service_name": "order-service",
        "title": "订单接口大量返回 500",
        "severity": IncidentSeverity.P1,
        "status": IncidentStatus.OPEN,
        "fingerprint": "order-service:http-500",
        "version": 1,
        "started_at": BASE_TIME,
        "resolved_at": None,
        "closed_at": None,
        "created_at": BASE_TIME,
        "updated_at": BASE_TIME,
    }
    values.update(overrides)
    return Incident(**values)


def test_incident_normalizes_text_and_timezone_to_utc() -> None:
    china_time = datetime(2026, 1, 1, 16, 0, tzinfo=timezone(timedelta(hours=8)))

    incident = make_incident(
        service_name="  order-service  ",
        title="  订单接口大量返回 500  ",
        fingerprint="  order-service:http-500  ",
        started_at=china_time,
        created_at=china_time,
        updated_at=china_time,
    )

    assert incident.service_name == "order-service"
    assert incident.title == "订单接口大量返回 500"
    assert incident.fingerprint == "order-service:http-500"
    assert incident.started_at == BASE_TIME
    assert incident.started_at.tzinfo is UTC


@pytest.mark.parametrize("field_name", ["started_at", "created_at", "updated_at"])
def test_incident_rejects_naive_datetime(field_name: str) -> None:
    with pytest.raises(IncidentValidationError, match="必须包含时区"):
        make_incident(**{field_name: datetime(2026, 1, 1, 8, 0)})


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("service_name", " "),
        ("title", " "),
        ("fingerprint", " "),
        ("service_name", "s" * 101),
        ("title", "t" * 201),
        ("fingerprint", "f" * 201),
    ],
)
def test_incident_rejects_invalid_required_text(field_name: str, value: str) -> None:
    with pytest.raises(IncidentValidationError):
        make_incident(**{field_name: value})


@pytest.mark.parametrize("actor_type", [ActorType.HUMAN, ActorType.AGENT, ActorType.EXECUTOR])
def test_non_system_actor_requires_actor_id(actor_type: ActorType) -> None:
    with pytest.raises(IncidentValidationError, match="必须提供 actor_id"):
        IncidentActor(actor_type=actor_type)


def test_actor_normalizes_identifier() -> None:
    human = IncidentActor(actor_type=ActorType.HUMAN, actor_id="  engineer-001  ")
    system = IncidentActor(actor_type=ActorType.SYSTEM, actor_id=" ")

    assert human.actor_id == "engineer-001"
    assert system.actor_id is None


def test_resolved_status_requires_resolved_time() -> None:
    with pytest.raises(IncidentValidationError, match="必须记录 resolved_at"):
        make_incident(status=IncidentStatus.RESOLVED)


def test_open_status_rejects_resolved_time() -> None:
    with pytest.raises(IncidentValidationError, match="不能保留 resolved_at"):
        make_incident(resolved_at=BASE_TIME)


def test_closed_status_requires_closed_time() -> None:
    with pytest.raises(IncidentValidationError, match="必须记录 closed_at"):
        make_incident(status=IncidentStatus.CLOSED, resolved_at=BASE_TIME)


def test_incident_rejects_invalid_version() -> None:
    with pytest.raises(IncidentValidationError, match="version"):
        make_incident(version=0)
