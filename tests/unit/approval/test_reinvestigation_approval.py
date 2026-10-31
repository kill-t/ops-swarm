"""回流审批请求的严格确认、原期限及独立阶段有效性。"""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.approval.reinvestigation_candidate.contracts import (
    ReflowRequestCommand,
    ReflowRequestError,
    ReflowRequestRecord,
)
from opsswarm.approval.reinvestigation_candidate.policy import request_availability, request_expiry

NOW = datetime(2026, 10, 31, 4, tzinfo=UTC)


def command_data(**changes):
    return {
        "action": "request_approval",
        "plan_content_sha256": "a" * 64,
        "expected_incident_version": 2,
        "acknowledge_unresolved_conditions": True,
        "idempotency_key": "reflow-approval-command-140",
        "reason": "已阅读本次新草案的未决条件，请求独立审批",
    } | changes


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_incident_version": True},
        {"expected_incident_version": "2"},
        {"expected_incident_version": 2.0},
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
        {"expected_wait_version": None},
        {"credential_expires_at": "2099-01-01T00:00:00Z"},
        {"expires_at": "2099-01-01T00:00:00Z"},
        {"execution_eligible": True},
    ],
)
def test_command_rejects_coercion_and_client_authority(changes):
    with pytest.raises(ValidationError):
        ReflowRequestCommand.model_validate(command_data(**changes))


def test_explicit_true_acknowledgement_is_required():
    body = command_data(reason="审" * 500)
    assert len(ReflowRequestCommand.model_validate(body).reason) == 500
    del body["acknowledge_unresolved_conditions"]
    with pytest.raises(ValidationError):
        ReflowRequestCommand.model_validate(body)


def test_expiry_is_capped_by_original_run_and_thirty_minutes():
    assert request_expiry(
        started_at=NOW - timedelta(seconds=299), max_duration_seconds=300, now=NOW
    ) == NOW + timedelta(seconds=1)
    local = NOW.astimezone(timezone(timedelta(hours=8)))
    expiry = request_expiry(started_at=local, max_duration_seconds=7200, now=local)
    assert expiry == NOW + timedelta(minutes=30) and expiry.tzinfo is UTC


@pytest.mark.parametrize("offset", [300, 301])
def test_expired_run_cannot_create_a_request(offset):
    with pytest.raises(ReflowRequestError):
        request_expiry(
            started_at=NOW - timedelta(seconds=offset), max_duration_seconds=300, now=NOW
        )


@pytest.mark.parametrize("field", ["started_at", "now"])
def test_expiry_requires_aware_time(field):
    values = {"started_at": NOW, "now": NOW}
    values[field] = NOW.replace(tzinfo=None)
    with pytest.raises(ValueError, match="时区"):
        request_expiry(**values, max_duration_seconds=300)


def request_record(**changes):
    run_id = uuid4()
    return ReflowRequestRecord(
        **(
            {
                "id": uuid4(),
                "investigation_run_id": run_id,
                "candidate_job_id": run_id,
                "plan_id": uuid4(),
                "plan_content_sha256": "a" * 64,
                "previous_incident_version": 2,
                "incident_version": 3,
                "basis_event_cursor": 4,
                "event_cursor": 5,
                "status": "pending",
                "policy_version": "reinvestigation-approval-v1",
                "requested_by_id": "engineer",
                "authorization_id": uuid4(),
                "idempotency_key": "request-record-140",
                "command_sha256": "b" * 64,
                "created_at": NOW - timedelta(seconds=10),
                "expires_at": NOW + timedelta(minutes=5),
                "credential_expires_at": NOW - timedelta(seconds=1),
            }
            | changes
        )
    )


def availability(record, **changes):
    return request_availability(
        record,
        **(
            {
                "basis_current": True,
                "now": NOW,
                "run_deadline": NOW + timedelta(minutes=5),
            }
            | changes
        ),
    )


def test_own_incident_transition_and_old_requester_expiry_do_not_self_invalidate():
    record = request_record()
    before = record.model_dump(mode="json")
    assert availability(record) == "current"
    assert record.status == "pending" and record.credential_expires_at < NOW
    assert record.previous_incident_version + 1 == record.incident_version
    assert record.model_dump(mode="json") == before


def test_changed_basis_is_stale_but_history_is_immutable():
    record = request_record()
    before = record.model_dump(mode="json")
    assert availability(record, basis_current=False) == "stale"
    assert record.model_dump(mode="json") == before


@pytest.mark.parametrize("deadline_kind", ["request", "run"])
def test_deadline_boundary_takes_precedence_over_stale_basis(deadline_kind):
    record = request_record(expires_at=NOW + timedelta(minutes=10))
    arguments = {"basis_current": False, "now": NOW + timedelta(minutes=5)}
    if deadline_kind == "request":
        record = request_record(expires_at=arguments["now"])
        arguments["run_deadline"] = NOW + timedelta(minutes=10)
    assert availability(record, **arguments) == "expired"


@pytest.mark.parametrize("status", ["approved", "rejected", "expired"])
def test_request_cannot_persist_an_approval_decision(status):
    with pytest.raises(ValidationError):
        request_record(status=status)
