"""回流决定的严格确认、完整新草案、状态时限和可信准入。"""

import hashlib
import json
from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.approval.reinvestigation_decision.authorization import (
    OPERATION,
    ROUTE,
    validate_admission,
)
from opsswarm.approval.reinvestigation_decision.contracts import (
    POLICY_VERSION,
    ReflowDecisionCommand,
    ReflowDecisionContent,
    ReflowDecisionError,
    ReflowDecisionRecord,
)
from opsswarm.approval.reinvestigation_decision.policy import (
    build_content,
    decision_availability,
    target,
    validate_timing,
    validate_transition,
)
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_routing.contracts import UnresolvedConstraint
from opsswarm.planning.reinvestigation_candidate.contracts import ReflowPlanRecord
from opsswarm.planning.reinvestigation_candidate.policy import build_plan
from tests.unit.approval.test_reinvestigation_approval import NOW, request_record
from tests.unit.investigation.test_reinvestigation_candidate import source_fixture
from tests.unit.planning.test_reinvestigation_candidate_plan import result_fixture


@pytest.fixture
def original_records():
    source = source_fixture()
    result = result_fixture(source)
    content = build_plan(source.investigation_run_id, source, result, content_hash(result))
    plan = ReflowPlanRecord(
        id=uuid4(),
        investigation_run_id=source.investigation_run_id,
        candidate_job_id=source.investigation_run_id,
        candidate_result_sha256=content_hash(result),
        routing_sha256=source.routing_sha256,
        incident_version=2,
        policy_version="reinvestigation-candidate-plan-v1",
        requested_by_id="same-subject",
        authorization_id=uuid4(),
        idempotency_key="plan-record-141",
        command_sha256="a" * 64,
        content=content,
        content_sha256=content_hash(content),
        credential_expires_at=NOW + timedelta(minutes=1),
        created_at=NOW - timedelta(minutes=1),
    )
    request = request_record(
        investigation_run_id=source.investigation_run_id,
        candidate_job_id=source.investigation_run_id,
        plan_id=plan.id,
        plan_content_sha256=plan.content_sha256,
        requested_by_id="same-subject",
    )
    return request, plan


def command_data(request=None, **changes):
    return {
        "action": "approve",
        "request_id": str(request.id if request else uuid4()),
        "plan_content_sha256": request.plan_content_sha256 if request else "a" * 64,
        "expected_incident_version": request.incident_version if request else 3,
        "acknowledge_unresolved_conditions": True,
        "idempotency_key": "reflow-decision-141",
        "reason": "保留全部原前置条件及未解问题，记录人工决定",
    } | changes


def test_large_legal_plan_keeps_all_constraints_with_outer_binding_space(original_records):
    request, plan = original_records
    data = plan.content.model_dump(mode="json")
    data["unresolved_constraints"] = []

    def size(value):
        # PostgreSQL jsonb正文包含分隔空格，排序不改变字节数。
        return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))

    while size(data) < 65500:
        data["unresolved_constraints"].append(
            UnresolvedConstraint(event_id=uuid4(), item_index=0, text="x" * 1000).model_dump(
                mode="json"
            )
        )
    extra = size(data) - 65500
    for item in reversed(data["unresolved_constraints"]):
        trim = min(extra, len(item["text"]) - 1)
        item["text"] = item["text"][:-trim] if trim else item["text"]
        extra -= trim
        if not extra:
            break
    large = type(plan.content).model_validate(data)
    digest = content_hash(large)
    plan = plan.model_copy(update={"content": large, "content_sha256": digest})
    request = request.model_copy(update={"plan_content_sha256": digest})
    content = build_content(request, plan, action="approve")
    assert size(data) == 65500 < 65536
    assert 65536 < size(content.model_dump(mode="json")) < 131072
    assert content.plan == large
    assert content.execution_eligible is False


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_incident_version": True},
        {"expected_incident_version": "3"},
        {"expected_incident_version": 3.0},
        {"expected_incident_version": 0},
        {"acknowledge_unresolved_conditions": 1},
        {"acknowledge_unresolved_conditions": "true"},
        {"acknowledge_unresolved_conditions": None},
        {"acknowledge_unresolved_conditions": False},
        {"action": "reject"},
        {"action": "expire"},
        {"action": "request_approval"},
        {"plan_content_sha256": "A" * 64},
        {"request_id": "unknown"},
        {"idempotency_key": "short"},
        {"reason": " "},
        {"reason": "审" * 501},
        {"expected_wait_version": 2},
        {"parameters": {"target_version": "v1"}},
        {"execution_eligible": True},
        {"credential_expires_at": "2099-01-01T00:00:00Z"},
    ],
)
def test_command_rejects_coercion_and_authority_overrides(changes):
    with pytest.raises(ValidationError):
        ReflowDecisionCommand.model_validate(command_data(**changes))


def test_action_is_required_and_negative_confirmation_defaults_false():
    data = command_data()
    del data["action"]
    with pytest.raises(ValidationError):
        ReflowDecisionCommand.model_validate(data)
    del data["acknowledge_unresolved_conditions"]
    for action in ("reject", "expire"):
        assert not ReflowDecisionCommand.model_validate(
            data | {"action": action}
        ).acknowledge_unresolved_conditions
    with pytest.raises(ValidationError):
        ReflowDecisionCommand.model_validate(data | {"action": "approve"})


@pytest.mark.parametrize("action", ["approve", "reject", "expire"])
def test_content_keeps_whole_new_plan_and_cannot_authorize_execution(original_records, action):
    request, plan = original_records
    before = request.model_dump(), plan.model_dump()
    content = build_content(request, plan, action=action)
    assert content.request_id == request.id and content.plan_id == plan.id
    assert content.plan_content_sha256 == plan.content_sha256
    assert content.plan == plan.content
    assert content.outcome == target(action)[0]
    assert content.execution_eligible is content.plan.execution_eligible is False
    assert content.plan.approval_eligible is False
    assert before == (request.model_dump(), plan.model_dump())
    with pytest.raises(ValidationError):
        ReflowDecisionContent.model_validate(
            content.model_dump(mode="json") | {"execution_eligible": True}
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_id", uuid4()),
        ("investigation_run_id", uuid4()),
        ("candidate_job_id", uuid4()),
        ("plan_content_sha256", "0" * 64),
        ("previous_incident_version", 1),
    ],
)
def test_content_rejects_cross_bound_request_or_plan(original_records, field, value):
    request, plan = original_records
    with pytest.raises(ReflowDecisionError):
        build_content(request.model_copy(update={field: value}), plan, action="approve")


def test_erased_conditions_with_old_hash_cannot_be_accepted(original_records):
    request, plan = original_records
    forged = plan.content.model_copy(update={"candidate_unresolved_questions": ()})
    with pytest.raises(ReflowDecisionError):
        build_content(request, plan.model_copy(update={"content": forged}), action="approve")


def transition(request, *, action="approve", availability="current", **changes):
    command = ReflowDecisionCommand.model_validate(
        command_data(request, action=action, acknowledge_unresolved_conditions=action == "approve")
    )
    validate_transition(
        command,
        request,
        **(
            {
                "availability": availability,
                "incident_status": "awaiting_approval",
                "incident_version": request.incident_version,
                "now": NOW,
            }
            | changes
        ),
    )


@pytest.mark.parametrize(
    "action,availability,offset",
    [
        ("approve", "current", 0),
        ("reject", "current", 0),
        ("reject", "stale", 0),
        ("expire", "expired", 300),
        ("expire", "stale", 301),
    ],
)
def test_actions_accept_their_own_phase_and_deadline(
    original_records, action, availability, offset
):
    request, _ = original_records
    transition(
        request, action=action, availability=availability, now=NOW + timedelta(seconds=offset)
    )


@pytest.mark.parametrize("action", ["approve", "reject", "expire"])
def test_all_actions_require_exact_incident_phase_and_version(original_records, action):
    request, _ = original_records
    with pytest.raises(ReflowDecisionError):
        transition(
            request,
            action=action,
            incident_version=request.incident_version + 1,
            now=request.expires_at,
        )
    with pytest.raises(ReflowDecisionError):
        transition(request, action=action, incident_status="investigating", now=request.expires_at)


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_deadline_equality_requires_explicit_expire(original_records, action):
    request, _ = original_records
    with pytest.raises(ReflowDecisionError) as caught:
        transition(request, action=action, now=request.expires_at)
    assert caught.value.code == "reflow_decision_expired"


def test_early_expire_and_stale_approve_are_rejected(original_records):
    request, _ = original_records
    with pytest.raises(ReflowDecisionError) as caught:
        transition(request, action="expire", now=request.expires_at - timedelta(microseconds=1))
    assert caught.value.code == "reflow_decision_not_expired"
    with pytest.raises(ReflowDecisionError) as caught:
        transition(request, availability="stale")
    assert caught.value.code == "reflow_decision_source_stale"


def test_reject_requires_an_existing_unexpired_request_phase(original_records):
    request, _ = original_records
    with pytest.raises(ReflowDecisionError):
        transition(request, action="reject", availability="pending")


@pytest.mark.parametrize("action", ["approve", "reject", "expire"])
def test_timing_rejects_naive_time(original_records, action):
    request, _ = original_records
    with pytest.raises(ValueError, match="时区"):
        validate_timing(action, expires_at=request.expires_at, now=NOW.replace(tzinfo=None))


def decision_record(request, plan, action="approve"):
    content = build_content(request, plan, action=action)
    return ReflowDecisionRecord(
        id=uuid4(),
        investigation_run_id=request.investigation_run_id,
        candidate_job_id=request.candidate_job_id,
        request_id=request.id,
        plan_content_sha256=request.plan_content_sha256,
        basis_event_cursor=request.event_cursor,
        previous_incident_version=request.incident_version,
        incident_version=request.incident_version + 1,
        event_cursor=6,
        outcome=content.outcome,
        policy_version=POLICY_VERSION,
        decided_by_id="same-subject",
        authorization_id=uuid4(),
        idempotency_key="decision-record-141",
        command_sha256="c" * 64,
        content=content,
        content_sha256=content_hash(content),
        credential_expires_at=NOW - timedelta(seconds=1),
        created_at=NOW - timedelta(seconds=5),
    )


def test_approved_projection_uses_own_basis_and_not_old_requester_credential(original_records):
    request, plan = original_records
    record = decision_record(request, plan)
    assert (
        decision_availability(record, basis_current=True, expires_at=request.expires_at, now=NOW)
        == "current"
    )
    assert (
        decision_availability(record, basis_current=False, expires_at=request.expires_at, now=NOW)
        == "stale"
    )
    assert (
        decision_availability(
            record, basis_current=False, expires_at=request.expires_at, now=request.expires_at
        )
        == "expired"
    )


@pytest.mark.parametrize("action", ["reject", "expire"])
def test_negative_decision_is_recorded_without_current_capacity(original_records, action):
    request, plan = original_records
    record = decision_record(request, plan, action)
    assert (
        decision_availability(
            record,
            basis_current=False,
            expires_at=request.expires_at,
            now=request.expires_at + timedelta(days=1),
        )
        == "recorded"
    )


@pytest.mark.parametrize(
    "mutation", ["role", "permission", "operation", "route", "run", "body", "time"]
)
def test_admission_requires_same_trusted_approver_and_original_body(original_records, mutation):
    request, _ = original_records
    command = ReflowDecisionCommand.model_validate(command_data(request))
    raw = command.model_dump_json()
    run_id = request.investigation_run_id
    values = {
        "id": uuid4(),
        "policy_version": "operator-auth-v1",
        "subject_id": "same-subject",
        "credential_id": "approver",
        "role": "approver",
        "permissions": ["decide_approval"],
        "operation": OPERATION,
        "method": "POST",
        "route": ROUTE,
        "resources": {"run_id": str(run_id)},
        "body_sha256": hashlib.sha256(raw.encode()).hexdigest(),
        "authorized_at": NOW,
    }
    changes = {
        "role": {"role": "engineer"},
        "permission": {"permissions": []},
        "operation": {"operation": "decide_candidate_approval"},
        "route": {"route": ROUTE + "/commands"},
        "run": {"resources": {"run_id": str(uuid4())}},
        "body": {"body_sha256": "0" * 64},
        "time": {"authorized_at": NOW + timedelta(microseconds=1)},
    }
    validate_admission(
        OperatorAuthorizationRow(**values),
        run_id=run_id,
        job_id=run_id,
        subject_id="same-subject",
        command=command,
        raw_body=raw,
        occurred_at=NOW,
    )
    with pytest.raises(ReflowDecisionError):
        validate_admission(
            OperatorAuthorizationRow(**(values | changes[mutation])),
            run_id=run_id,
            job_id=run_id,
            subject_id="same-subject",
            command=command,
            raw_body=raw,
            occurred_at=NOW,
        )
