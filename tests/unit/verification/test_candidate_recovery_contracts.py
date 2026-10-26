"""恢复观测必须保持严格身份、时间与三样本同尝试边界。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.verification.candidate.contracts import (
    ProbeObservation,
    RecoveryError,
    RecoveryJobRecord,
    RecoveryResultContent,
    RecoverySampleRecord,
)
from opsswarm.verification.candidate.policy import classify, validate_observation

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def observation(**changes: object) -> ProbeObservation:
    values: dict[str, object] = {
        "requested_at": NOW,
        "observed_at": NOW,
        "http_status": 200,
        "value": 0.0,
        "order_id": "opsswarm-verification-probe",
    }
    values.update(changes)
    return ProbeObservation.model_validate(values)


def samples(*, failed: bool = False) -> tuple[RecoverySampleRecord, ...]:
    job_id, attempt_id = uuid4(), uuid4()
    result = []
    for sequence in (1, 2, 3):
        timestamp = NOW + timedelta(seconds=(sequence - 1) * 2)
        item = observation(requested_at=timestamp, observed_at=timestamp)
        if failed and sequence == 2:
            item = observation(
                requested_at=timestamp,
                observed_at=timestamp,
                http_status=503,
                value=1.0,
                order_id=None,
            )
        result.append(
            RecoverySampleRecord(
                job_id=job_id,
                attempt_id=attempt_id,
                sequence=sequence,
                observation=item,
                error_code=None,
                created_at=timestamp,
            )
        )
    return tuple(result)


@pytest.mark.parametrize(
    "changes",
    [
        {"http_status": True},
        {"http_status": "200"},
        {"http_status": 200.0},
        {"value": False},
        {"value": "0"},
        {"value": float("nan")},
        {"value": 1.0},
        {"http_status": 302},
        {"order_id": None},
        {"order_id": "other-order"},
        {"requested_at": True},
        {"requested_at": 12345},
        {"requested_at": "12345"},
        {"requested_at": NOW.replace(tzinfo=None)},
        {"requested_at": "2026-10-26T08:00:00+08:00"},
        {"requested_at": NOW + timedelta(seconds=1)},
        {"body": "private payload"},
    ],
)
def test_observation_rejects_invalid_scalar_identity_and_time(changes: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        observation(**changes)


def test_observation_is_frozen_and_accepts_utc_iso_json() -> None:
    item = observation(requested_at="2026-10-26T00:00:00Z")
    assert ProbeObservation.model_validate_json(item.model_dump_json()) == item
    with pytest.raises(ValidationError):
        item.value = 1.0


def test_validation_rechecks_constructed_objects_and_freshness() -> None:
    item = observation()
    assert validate_observation(item, NOW) == item
    forged = item.model_copy(update={"http_status": True})
    with pytest.raises(ValidationError):
        validate_observation(forged, NOW)
    for now in (NOW - timedelta(seconds=1), NOW + timedelta(seconds=11)):
        with pytest.raises(RecoveryError, match="时效"):
            validate_observation(item, now)
    with pytest.raises(RecoveryError):
        validate_observation(observation(requested_at=NOW - timedelta(seconds=11)), NOW)


@pytest.mark.parametrize(
    ("execution", "expected"),
    [
        ("unknown", "execution_unknown"),
        ("not_applied", "not_executed"),
        ("blocked", "not_executed"),
        ("applied_superseded", "not_recovered"),
    ],
)
def test_execution_status_short_circuits_probe(execution: str, expected: str) -> None:
    assert classify(execution, ())[0] == expected


def test_three_healthy_business_samples_recover_and_any_5xx_fails() -> None:
    assert classify("applied_current", samples())[0] == "recovered"
    assert classify("applied_current", samples(failed=True))[0] == "not_recovered"


def test_incomplete_error_exhaustion_mixed_attempts_or_fast_samples_fail_source() -> None:
    items = samples()
    cases = [
        (),
        items[:2],
        (items[0], items[1].model_copy(update={"attempt_id": uuid4()}), items[2]),
        (items[0], items[1].model_copy(update={"sequence": 1}), items[2]),
        (
            items[0],
            items[1].model_copy(update={"observation": observation(), "created_at": NOW}),
            items[2],
        ),
        (items[0].model_copy(update={"observation": None, "error_code": "probe_timeout"}),),
    ]
    for case in cases:
        assert classify("applied_current", case)[0] == "source_failed"
    assert classify("applied_current", items, exhausted=True)[0] == "source_failed"
    with pytest.raises(RecoveryError):
        classify("invented", ())


def test_sample_requires_exactly_one_observation_or_error() -> None:
    item = samples()[0]
    for values in (
        {"observation": None, "error_code": None},
        {"error_code": "probe_timeout"},
        {"sequence": True},
        {"sequence": 4},
    ):
        with pytest.raises(ValidationError):
            RecoverySampleRecord.model_validate({**item.model_dump(), **values})


def test_result_parent_flag_requires_actual_false_and_job_bounds_are_strict() -> None:
    result = {
        "execution_job_id": uuid4(),
        "execution_result_sha256": "a" * 64,
        "operation_id": uuid4(),
        "outcome": "execution_unknown",
        "attempt_id": None,
        "reason_code": "execution_unknown",
        "created_at": NOW,
    }
    assert RecoveryResultContent.model_validate(result).parent_advanced is False
    for flag in (0, "false", True):
        with pytest.raises(ValidationError):
            RecoveryResultContent.model_validate({**result, "parent_advanced": flag})
    job = {
        "id": uuid4(),
        "investigation_run_id": uuid4(),
        "candidate_job_id": uuid4(),
        "verification_version": 1,
        "operation_id": uuid4(),
        "execution_result_sha256": "b" * 64,
        "window_start": NOW,
        "window_end": NOW + timedelta(seconds=120),
        "status": "queued",
        "attempt_count": 0,
        "created_at": NOW,
        "updated_at": NOW,
    }
    assert RecoveryJobRecord.model_validate(job).attempt_count == 0
    for count in (True, -1, 4, "1"):
        with pytest.raises(ValidationError):
            RecoveryJobRecord.model_validate({**job, "attempt_count": count})
