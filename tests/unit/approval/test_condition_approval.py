"""独立核验审批的精确引用、动作期限及负面收敛边界。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.approval.conditions.contracts import (
    POLICY_VERSION,
    ApprovalAuditRecord,
    ApprovalError,
    DecisionCommand,
    DecisionContent,
    RequestCommand,
    RequestContent,
    RequestRecord,
)
from opsswarm.approval.conditions.policy import (
    build_decision_content,
    build_request_content,
    decision_availability,
    request_availability,
    validate_decision,
    validate_request,
    validate_timing,
)
from opsswarm.investigation.context.hashing import content_hash
from tests.unit.execution.test_candidate_conditions import NOW, decision_for, plan_for, record_for


def verification_for():
    plan = plan_for()
    return record_for(plan, decision_for(plan))


def request_command_for(verification, **changes):
    return RequestCommand.model_validate(
        {
            "verification_id": verification.id,
            "verification_content_sha256": verification.content_sha256,
            "expected_incident_version": verification.incident_version,
            "idempotency_key": "condition-request-1",
            "reason": "请求审核完整核验",
            **changes,
        }
    )


def request_for(verification=None, **changes):
    verification = verification or verification_for()
    content = build_request_content(verification)
    command = request_command_for(verification)
    return RequestRecord.model_validate(
        {
            "id": uuid4(),
            "investigation_run_id": verification.investigation_run_id,
            "candidate_job_id": verification.candidate_job_id,
            "verification_id": verification.id,
            "verification_content_sha256": verification.content_sha256,
            "verification_version": verification.version,
            "incident_version": verification.incident_version,
            "policy_version": POLICY_VERSION,
            "status": "pending",
            "requested_by_id": "engineer-1",
            "authorization_id": uuid4(),
            "idempotency_key": command.idempotency_key,
            "command_sha256": content_hash(command),
            "content": content,
            "content_sha256": content_hash(content),
            "created_at": NOW,
            "expires_at": verification.expires_at,
            **changes,
        }
    )


def decision_command_for(request, action="approve", **changes):
    return DecisionCommand.model_validate(
        {
            "action": action,
            "request_id": request.id,
            "request_content_sha256": request.content_sha256,
            "expected_incident_version": request.incident_version,
            "acknowledge_verification": action == "approve",
            "idempotency_key": "condition-decision-1",
            "reason": "记录人工判断",
            **changes,
        }
    )


@pytest.mark.parametrize("value", [0, 1, "false", "true", None, True])
def test_contents_reject_coerced_or_enabled_execution(value):
    request = request_for()
    for content in (request.content, build_decision_content(request, "approve")):
        with pytest.raises(ValidationError):
            type(content).model_validate({**content.model_dump(), "execution_eligible": value})


@pytest.mark.parametrize("value", [0, 1, "true", "false", None])
def test_confirmation_requires_actual_boolean(value):
    with pytest.raises(ValidationError):
        decision_command_for(request_for(), acknowledge_verification=value)


@pytest.mark.parametrize("action,ack", [("approve", False), ("reject", True), ("expire", True)])
def test_only_approval_requires_explicit_confirmation(action, ack):
    with pytest.raises(ValidationError):
        decision_command_for(request_for(), action, acknowledge_verification=ack)


@pytest.mark.parametrize("value", [True, 1, "4", 4.0])
def test_commands_reject_implicit_or_out_of_range_incident_version(value):
    verification = verification_for()
    with pytest.raises(ValidationError):
        request_command_for(verification, expected_incident_version=value)
    with pytest.raises(ValidationError):
        decision_command_for(request_for(verification), expected_incident_version=value)


def test_request_and_all_decisions_keep_complete_exact_refs_and_original_flags():
    verification = verification_for()
    original = verification.model_dump()
    request = request_for(verification)
    content = request.content
    assert content.verification_id == verification.id
    assert content.verification_version == verification.version
    assert content.verification_content_sha256 == verification.content_sha256
    assert content.base_decision_id == verification.decision_id
    assert content.base_decision_content_sha256 == verification.decision_content_sha256
    assert content.plan_id == verification.content.plan_id
    assert content.plan_content_sha256 == verification.content.plan_content_sha256
    for action, outcome in (("approve", "approved"), ("reject", "rejected"), ("expire", "expired")):
        result = build_decision_content(request, action)
        assert result.outcome == outcome
        assert result.request_id == request.id
        assert result.request_content_sha256 == request.content_sha256
        assert result.model_dump(
            exclude={"version", "request_id", "request_content_sha256", "outcome"}
        ) == (content.model_dump(exclude={"version"}))
        assert result.execution_eligible is False
    assert verification.model_dump() == original
    assert verification.content.approval_required is True
    assert verification.content.execution_eligible is False


@pytest.mark.parametrize("status", ["blocked", "plan_revision_required"])
def test_request_rejects_non_ready_status_even_with_all_verified(status):
    verification = verification_for()
    content = verification.content.model_copy(update={"status": status})
    verification = verification.model_copy(
        update={"content": content, "content_sha256": content_hash(content)}
    )
    with pytest.raises(ApprovalError, match="全部verified"):
        build_request_content(verification)


@pytest.mark.parametrize("status", ["unresolved", "not_applicable"])
def test_claimed_ready_status_cannot_hide_nonverified_item(status):
    verification = verification_for()
    items = verification.content.items
    content = verification.content.model_copy(
        update={"items": (items[0].model_copy(update={"status": status}), *items[1:])}
    )
    verification = verification.model_copy(
        update={"content": content, "content_sha256": content_hash(content)}
    )
    with pytest.raises(ApprovalError):
        build_request_content(verification)


@pytest.mark.parametrize(
    "changes",
    [{"content_sha256": "f" * 64}, {"decision_id": uuid4()}, {"decision_content_sha256": "f" * 64}],
)
def test_request_rejects_broken_source_digest_and_internal_refs(changes):
    with pytest.raises(ApprovalError):
        build_request_content(verification_for().model_copy(update=changes))


@pytest.mark.parametrize(
    "changes",
    [
        {"verification_id": uuid4()},
        {"verification_content_sha256": "f" * 64},
        {"expected_incident_version": 5},
    ],
)
def test_request_command_must_match_exact_verification_and_incident(changes):
    verification = verification_for()
    with pytest.raises(ApprovalError):
        validate_request(
            request_command_for(verification, **changes),
            verification,
            availability="current",
            now=NOW,
        )


@pytest.mark.parametrize("availability", ["pending", "superseded", "stale", "expired", "recorded"])
def test_request_requires_current_exact_version(availability):
    verification = verification_for()
    with pytest.raises(ApprovalError):
        validate_request(
            request_command_for(verification), verification, availability=availability, now=NOW
        )


@pytest.mark.parametrize("offset,allowed", [(-1, False), (0, True), (299, True), (300, False)])
def test_request_creation_time_is_within_original_verification(offset, allowed):
    verification = verification_for()
    args = (request_command_for(verification), verification)
    if allowed:
        validate_request(*args, availability="current", now=NOW + timedelta(seconds=offset))
    else:
        with pytest.raises(ApprovalError):
            validate_request(*args, availability="current", now=NOW + timedelta(seconds=offset))


@pytest.mark.parametrize(
    "changes",
    [
        {"content_sha256": "f" * 64},
        {"verification_id": uuid4()},
        {"verification_content_sha256": "f" * 64},
        {"verification_version": 2},
    ],
)
def test_decision_rejects_broken_request_even_for_negative_actions(changes):
    request = request_for().model_copy(update=changes)
    for action in ("approve", "reject", "expire"):
        with pytest.raises(ApprovalError):
            build_decision_content(request, action)
        with pytest.raises(ApprovalError):
            validate_decision(
                decision_command_for(request, action), request, availability="stale", now=NOW
            )


@pytest.mark.parametrize(
    "changes",
    [
        {"request_id": uuid4()},
        {"request_content_sha256": "f" * 64},
        {"expected_incident_version": 5},
    ],
)
def test_all_actions_must_bind_request_and_original_incident_version(changes):
    request = request_for()
    for action in ("approve", "reject", "expire"):
        now = request.expires_at if action == "expire" else NOW
        with pytest.raises(ApprovalError):
            validate_decision(
                decision_command_for(request, action, **changes),
                request,
                availability="current",
                now=now,
            )


@pytest.mark.parametrize("availability", ["pending", "superseded", "stale", "expired", "recorded"])
def test_approve_requires_current_source_and_negative_decisions_can_converge(availability):
    request = request_for()
    with pytest.raises(ApprovalError):
        validate_decision(
            decision_command_for(request), request, availability=availability, now=NOW
        )
    validate_decision(
        decision_command_for(request, "reject"), request, availability=availability, now=NOW
    )
    validate_decision(
        decision_command_for(request, "expire"),
        request,
        availability=availability,
        now=request.expires_at,
    )


@pytest.mark.parametrize("action", ["approve", "reject", "expire"])
@pytest.mark.parametrize("offset", [-1, 0, 299, 300, 301])
def test_all_action_deadlines_and_creation_boundaries(action, offset):
    request = request_for()
    now = NOW + timedelta(seconds=offset)
    allowed = offset >= 300 if action == "expire" else 0 <= offset < 300
    if allowed:
        validate_decision(
            decision_command_for(request, action), request, availability="current", now=now
        )
        validate_timing(action, request, now)
    else:
        with pytest.raises(ApprovalError):
            validate_decision(
                decision_command_for(request, action), request, availability="current", now=now
            )
        with pytest.raises(ApprovalError):
            validate_timing(action, request, now)


def test_late_postwrite_clock_uses_original_deadline_and_rejects_naive_time():
    request = request_for()
    validate_timing("approve", request, request.expires_at - timedelta(microseconds=1))
    with pytest.raises(ApprovalError):
        validate_timing("approve", request, request.expires_at)
    with pytest.raises(ValueError, match="时区"):
        validate_timing("expire", request, request.expires_at.replace(tzinfo=None))


@pytest.mark.parametrize("availability", ["current", "superseded", "stale", "expired"])
def test_views_preserve_original_applicability_and_record_negative_history(availability):
    assert request_availability(availability, False) == availability
    assert request_availability(availability, True) == "recorded"
    assert decision_availability("approved", availability) == availability
    assert decision_availability("rejected", availability) == "recorded"
    assert decision_availability("expired", availability) == "recorded"


def test_audit_command_union_must_match_stage():
    request = request_for()
    audit = {
        "cursor": 1,
        "stage": "request",
        "authorization_id": uuid4(),
        "investigation_run_id": request.investigation_run_id,
        "candidate_job_id": request.candidate_job_id,
        "verification_version": 1,
        "subject_id": "engineer-1",
        "command": request_command_for(verification_for()),
        "command_sha256": "a" * 64,
        "outcome": "applied",
        "http_status": 201,
        "request_id": request.id,
        "decision_id": None,
        "error_code": None,
        "error_message": None,
        "created_at": NOW,
    }
    assert isinstance(ApprovalAuditRecord.model_validate(audit).command, RequestCommand)
    with pytest.raises(ValidationError, match="审计阶段"):
        ApprovalAuditRecord.model_validate({**audit, "stage": "decision"})
    command = decision_command_for(request)
    assert isinstance(
        ApprovalAuditRecord.model_validate(
            {**audit, "stage": "decision", "command": command, "decision_id": uuid4()}
        ).command,
        DecisionCommand,
    )
    with pytest.raises(ValidationError, match="审计阶段"):
        ApprovalAuditRecord.model_validate({**audit, "command": command})
    for change in (
        {"http_status": 200},
        {"http_status": 404},
        {"request_id": None},
        {"decision_id": uuid4()},
        {"error_code": "forged_error"},
        {"error_message": "伪造错误"},
        {"outcome": "replayed"},
        {"outcome": "error", "http_status": 409, "error_code": "conflict", "error_message": "冲突"},
    ):
        with pytest.raises(ValidationError, match="审计结果"):
            ApprovalAuditRecord.model_validate({**audit, **change})
    refused = {
        **audit,
        "outcome": "error",
        "http_status": 409,
        "request_id": None,
        "error_code": "conflict",
        "error_message": "冲突",
    }
    assert ApprovalAuditRecord.model_validate(refused).request_id is None
    for change in (
        {"http_status": 201},
        {"error_code": None},
        {"error_message": None},
        {"decision_id": uuid4()},
    ):
        with pytest.raises(ValidationError, match="审计结果"):
            ApprovalAuditRecord.model_validate({**refused, **change})


def test_content_contracts_reject_unknown_authority_fields_and_wrong_versions():
    request = request_for()
    for kind, content in (
        (RequestContent, request.content),
        (DecisionContent, build_decision_content(request, "approve")),
    ):
        with pytest.raises(ValidationError):
            kind.model_validate({**content.model_dump(), "version": "future-v2"})
        with pytest.raises(ValidationError):
            kind.model_validate({**content.model_dump(), "expires_at": request.expires_at})
