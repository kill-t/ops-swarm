"""可信审批请求正文、不可续期期限与独立阶段判断。"""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.approval.candidate.contracts import RequestCommand, RequestError, RequestRecord
from opsswarm.approval.candidate.policy import request_availability, request_expiry

NOW = datetime(2026, 10, 24, 4, tzinfo=UTC)


def command_data(**changes):
    return {
        "plan_content_sha256": "a" * 64,
        "expected_wait_version": None,
        "expected_incident_version": 1,
        "acknowledge_unresolved_conditions": True,
        "idempotency_key": "request-test-1",
        "reason": "已阅读草案前置条件与未解问题，请求独立审批",
        **changes,
    }


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_wait_version": True},
        {"expected_wait_version": "2"},
        {"expected_wait_version": 2.0},
        {"expected_wait_version": 1},
        {"expected_wait_version": 17},
        {"expected_incident_version": True},
        {"expected_incident_version": "1"},
        {"expected_incident_version": 1.0},
        {"expected_incident_version": 0},
        {"acknowledge_unresolved_conditions": False},
        {"acknowledge_unresolved_conditions": 1},
        {"acknowledge_unresolved_conditions": 1.0},
        {"acknowledge_unresolved_conditions": "true"},
        {"acknowledge_unresolved_conditions": None},
        {"plan_content_sha256": "A" * 64},
        {"idempotency_key": "short"},
        {"reason": " "},
        {"reason": "审" * 501},
        {"action": "approve"},
        {"expires_at": "2026-10-24T05:00:00Z"},
        {"parameters": {"target_version": "v1"}},
        {"approval_eligible": True},
        {"execution_eligible": True},
    ],
)
def test_request_command_rejects_coercion_and_client_authority(changes):
    with pytest.raises(ValidationError):
        RequestCommand.model_validate(command_data(**changes))


def test_request_command_requires_explicit_wait_version_and_acknowledgement():
    assert RequestCommand.model_validate(command_data()).expected_wait_version is None
    assert RequestCommand.model_validate(command_data(expected_wait_version=2)).action == (
        "request_approval"
    )
    assert len(RequestCommand.model_validate(command_data(reason="审" * 500)).reason) == 500
    for field in ("expected_wait_version", "acknowledge_unresolved_conditions"):
        data = command_data()
        del data[field]
        with pytest.raises(ValidationError):
            RequestCommand.model_validate(data)


def test_expiry_uses_remaining_original_deadline_instead_of_renewing_run():
    assert request_expiry(
        started_at=NOW - timedelta(seconds=299), max_duration_seconds=300, now=NOW
    ) == NOW + timedelta(seconds=1)


def test_expiry_is_capped_at_thirty_minutes_and_normalized_to_utc():
    local_time = NOW.astimezone(timezone(timedelta(hours=8)))
    expiry = request_expiry(started_at=local_time, max_duration_seconds=7200, now=local_time)
    assert expiry == NOW + timedelta(minutes=30)
    assert expiry.tzinfo is UTC


@pytest.mark.parametrize("offset", [300, 301])
def test_expired_run_cannot_create_a_new_request(offset):
    with pytest.raises(RequestError) as caught:
        request_expiry(
            started_at=NOW - timedelta(seconds=offset), max_duration_seconds=300, now=NOW
        )
    assert caught.value.code == "candidate_approval_deadline_exceeded"


@pytest.mark.parametrize("field", ["started_at", "now"])
def test_expiry_rejects_naive_time(field):
    values = {"started_at": NOW, "now": NOW}
    values[field] = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match="时区"):
        request_expiry(**values, max_duration_seconds=300)


def request_record(**changes):
    return RequestRecord(
        **{
            "id": uuid4(),
            "investigation_run_id": uuid4(),
            "candidate_job_id": uuid4(),
            "plan_id": uuid4(),
            "plan_content_sha256": "a" * 64,
            "wait_version": None,
            "previous_incident_version": 2,
            "incident_version": 3,
            "event_cursor": 5,
            "status": "pending",
            "policy_version": "candidate-approval-v1",
            "requested_by_id": "engineer",
            "authorization_id": uuid4(),
            "idempotency_key": "request-test-1",
            "command_sha256": "b" * 64,
            "created_at": NOW,
            "expires_at": NOW + timedelta(minutes=5),
            **changes,
        }
    )


def availability(record, **changes):
    return request_availability(
        record,
        **{
            "source_kind": "terminal_handoff",
            "run_status": "await_candidate",
            "latest_wait_version": None,
            "wait_cancelled": False,
            "incident_status": "awaiting_approval",
            "incident_version": 3,
            "now": NOW,
            "run_deadline": NOW + timedelta(minutes=5),
            **changes,
        },
    )


def test_own_request_approval_transition_does_not_make_request_stale():
    record = request_record()
    assert availability(record) == "current"
    assert record.status == "pending"
    assert record.previous_incident_version != record.incident_version


def test_human_request_is_current_only_at_bound_wait_version():
    record = request_record(wait_version=2)
    arguments = {"source_kind": "human_route", "run_status": "await_human"}
    assert availability(record, **arguments, latest_wait_version=2) == "current"
    assert availability(record, **arguments, latest_wait_version=3) == "stale"
    assert availability(record, **arguments, latest_wait_version=2, wait_cancelled=True) == "stale"


@pytest.mark.parametrize(
    "changes",
    [
        {"run_status": "running"},
        {"run_status": "cancelled"},
        {"run_status": "await_human"},
        {"incident_status": "investigating", "incident_version": 2},
        {"incident_status": "mitigating", "incident_version": 4},
        {"incident_status": "awaiting_approval", "incident_version": 4},
        {"incident_status": None, "incident_version": None},
    ],
)
def test_changed_phase_is_stale_without_mutating_request(changes):
    record = request_record()
    before = record.model_dump()
    assert availability(record, **changes) == "stale"
    assert record.model_dump() == before


@pytest.mark.parametrize("deadline_kind", ["request", "run"])
def test_deadline_boundary_is_expired_even_if_source_has_also_changed(deadline_kind):
    record = request_record(expires_at=NOW + timedelta(minutes=10))
    arguments = {"now": NOW + timedelta(minutes=5), "incident_status": "investigating"}
    if deadline_kind == "request":
        record = request_record(expires_at=arguments["now"])
        arguments["run_deadline"] = NOW + timedelta(minutes=10)
    assert availability(record, **arguments) == "expired"
    assert record.status == "pending"


@pytest.mark.parametrize("status", ["approved", "rejected", "expired"])
def test_request_fact_cannot_store_an_approval_decision(status):
    with pytest.raises(ValidationError):
        request_record(status=status)
