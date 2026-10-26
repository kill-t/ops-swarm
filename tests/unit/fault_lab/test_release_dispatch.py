"""派发窗口、规范操作键及不可改写终态的严格结构边界。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest
from pydantic import ValidationError

from opsswarm.fault_lab.release.dispatch_contracts import DispatchCommand, DispatchRecord
from opsswarm.fault_lab.release.policy import command_hash

NOW = datetime(2026, 10, 26, 8, tzinfo=UTC)
OPERATION = UUID(int=126)


def payload():
    return dict(
        operation_id=str(OPERATION),
        command=dict(
            expected_version="2026.01.01-rc1",
            expected_revision=2,
            target_version="stable",
            idempotency_key=f"candidate-release-{OPERATION}",
        ),
        not_before=NOW.isoformat(),
        expires_at=(NOW + timedelta(seconds=30)).isoformat(),
    )


def test_exact_window_and_timezone_are_normalized():
    raw = payload()
    raw["not_before"] = "2026-10-26T16:00:00+08:00"
    parsed = DispatchCommand.model_validate(raw)
    assert parsed.not_before == NOW and parsed.expires_at - parsed.not_before == timedelta(
        seconds=30
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("expires_at", NOW.isoformat()),
        ("expires_at", (NOW + timedelta(seconds=30, microseconds=1)).isoformat()),
        ("not_before", "2026-10-26T08:00:00"),
        ("not_before", 123),
        ("expires_at", "2026-10-26T08:00:30.1234567Z"),
        ("not_before", "0001-01-01T00:00:00+01:00"),
        ("actor_id", "injected"),
        ("authorization_id", str(UUID(int=1))),
    ],
)
def test_window_requires_exact_aware_times_and_no_extra_authority(field, value):
    raw = payload()
    raw[field] = value
    with pytest.raises(ValidationError):
        DispatchCommand.model_validate(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_version", "stable"),
        ("target_version", "2026.01.01-rc1"),
        ("expected_revision", True),
        ("expected_revision", 2.0),
        ("idempotency_key", "candidate-release-another-operation"),
        ("idempotency_key", f"candidate-release-{UUID(int=127)}"),
        ("url", "http://external.invalid"),
    ],
)
def test_only_exact_operation_rollback_is_accepted(field, value):
    raw = payload()
    raw["command"][field] = value
    with pytest.raises(ValidationError):
        DispatchCommand.model_validate(raw)


def record(**changes):
    raw = payload()
    return {
        **raw,
        "policy_version": "lab-release-dispatch-v1",
        "status": "prepared",
        "command_sha256": command_hash(raw["command"]),
        "command_id": None,
        "created_at": NOW,
        "finished_at": None,
        **changes,
    }


@pytest.mark.parametrize(
    "change",
    [
        {"status": "applied"},
        {"status": "cancelled"},
        {"status": "prepared", "finished_at": NOW},
        {"command_id": str(UUID(int=1))},
        {"command_sha256": "0" * 64},
        {"created_at": NOW - timedelta(microseconds=1)},
        {
            "status": "applied",
            "command_id": str(UUID(int=1)),
            "finished_at": NOW + timedelta(seconds=30),
        },
        {"policy_version": "lab-release-dispatch-v2"},
    ],
)
def test_record_rejects_forged_state_hash_or_deadline(change):
    with pytest.raises(ValidationError):
        DispatchRecord.model_validate(record(**change))


def test_cancel_after_expiry_is_a_recorded_close_and_valid_dtos_are_revalidated():
    closed = DispatchRecord.model_validate(
        record(status="cancelled", finished_at=NOW + timedelta(seconds=60))
    )
    assert closed.status == "cancelled"
    copied = DispatchCommand.model_validate(payload()).model_copy(update={"expires_at": NOW})
    with pytest.raises(ValidationError):
        DispatchCommand.model_validate(copied.model_dump())
