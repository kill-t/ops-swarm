from datetime import UTC, datetime, timedelta, timezone

import pytest

from opsswarm.incidents import (
    AlertSubmission,
    IncidentSeverity,
    IncidentValidationError,
)

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def make_submission(**overrides: object) -> AlertSubmission:
    values: dict[str, object] = {
        "source": "fault-simulator",
        "external_event_id": "alert-001",
        "fingerprint": "order-service:http-500",
        "service_name": "order-service",
        "severity": IncidentSeverity.P1,
        "title": "订单接口大量返回 500",
        "payload": {"status_code": 500},
        "occurred_at": BASE_TIME,
        "ingested_at": BASE_TIME + timedelta(seconds=1),
    }
    values.update(overrides)
    return AlertSubmission(**values)


def test_alert_submission_normalizes_text_and_utc_time() -> None:
    china_time = datetime(2026, 1, 1, 16, 0, tzinfo=timezone(timedelta(hours=8)))

    submission = make_submission(
        source="  fault-simulator ",
        external_event_id="  alert-001 ",
        occurred_at=china_time,
    )

    assert submission.source == "fault-simulator"
    assert submission.external_event_id == "alert-001"
    assert submission.occurred_at == BASE_TIME
    assert submission.payload == {"status_code": 500}


def test_alert_submission_copies_nested_payload() -> None:
    original_payload = {"context": {"attempt": 1}}

    submission = make_submission(payload=original_payload)
    original_payload["context"]["attempt"] = 2

    assert submission.payload == {"context": {"attempt": 1}}


@pytest.mark.parametrize(
    "payload",
    [
        {"invalid": object()},
        {"invalid": float("nan")},
    ],
)
def test_alert_submission_rejects_non_standard_json(payload: object) -> None:
    with pytest.raises(IncidentValidationError, match="标准 JSON"):
        make_submission(payload=payload)


def test_alert_submission_limits_serialized_payload_size() -> None:
    with pytest.raises(IncidentValidationError, match="不能超过"):
        make_submission(payload={"content": "故" * (64 * 1024)})


def test_alert_submission_rejects_naive_time() -> None:
    with pytest.raises(IncidentValidationError, match="必须包含时区"):
        make_submission(ingested_at=datetime(2026, 1, 1, 8, 0))
