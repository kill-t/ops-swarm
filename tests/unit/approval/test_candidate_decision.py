"""候选审批决定的严格输入、原约束保留、期限与独立阶段。"""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.approval.candidate.contracts import RequestRecord
from opsswarm.approval.decision.contracts import (
    POLICY_VERSION,
    DecisionCommand,
    DecisionContent,
    DecisionError,
    DecisionRecord,
)
from opsswarm.approval.decision.policy import (
    build_content,
    decision_availability,
    target,
    validate_timing,
    validate_transition,
)
from opsswarm.incidents.models import IncidentAction
from opsswarm.investigation.candidate.evaluation import evaluate_candidate
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.planning.candidate.contracts import PlanRecord
from opsswarm.planning.candidate.policy import build_plan
from tests.unit.investigation.test_candidate_evaluation import candidate, candidate_source

NOW = datetime(2026, 10, 24, 4, tzinfo=UTC)


@pytest.fixture
def original_records():
    source = candidate_source()
    result = evaluate_candidate(
        source,
        candidate(
            preconditions=("先核实现场条件", "保留人工约束"),
            unresolved_questions=("机制尚未证明", "反证仍待排除"),
        ),
    )
    run_id, job_id = uuid4(), uuid4()
    content = build_plan(job_id, source, result, content_hash(result))
    plan = PlanRecord(
        id=uuid4(),
        investigation_run_id=run_id,
        candidate_job_id=job_id,
        candidate_result_sha256=content_hash(result),
        wait_version=None,
        incident_version=2,
        policy_version="candidate-plan-v1",
        requested_by_id="engineer",
        authorization_id=uuid4(),
        idempotency_key="plan-test-1",
        command_sha256="a" * 64,
        content=content,
        content_sha256=content_hash(content),
        created_at=NOW - timedelta(minutes=1),
    )
    request = RequestRecord(
        id=uuid4(),
        investigation_run_id=run_id,
        candidate_job_id=job_id,
        plan_id=plan.id,
        plan_content_sha256=plan.content_sha256,
        wait_version=None,
        previous_incident_version=2,
        incident_version=3,
        event_cursor=5,
        policy_version="candidate-approval-v1",
        requested_by_id="engineer",
        authorization_id=uuid4(),
        idempotency_key="request-test-1",
        command_sha256="b" * 64,
        created_at=NOW,
        expires_at=NOW + timedelta(minutes=5),
    )
    return request, plan


def command_data(request=None, **changes):
    return {
        "action": "approve",
        "request_id": request.id if request else uuid4(),
        "plan_content_sha256": request.plan_content_sha256 if request else "a" * 64,
        "expected_incident_version": request.incident_version if request else 3,
        "acknowledge_unresolved_conditions": True,
        "idempotency_key": "decision-test-1",
        "reason": "已阅读全部原前置条件与未解问题，记录人工决定",
        **changes,
    }


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
        {"evidence_ids": ["invented-1"]},
        {"preconditions": []},
        {"execution_eligible": True},
    ],
)
def test_command_rejects_coercion_missing_acknowledgement_and_client_authority(changes):
    with pytest.raises(ValidationError):
        DecisionCommand.model_validate(command_data(**changes))


def test_action_is_required_and_negative_actions_default_to_false():
    payload = command_data()
    del payload["action"]
    with pytest.raises(ValidationError):
        DecisionCommand.model_validate(payload)
    del payload["acknowledge_unresolved_conditions"]
    for action in ("reject", "expire"):
        command = DecisionCommand.model_validate({**payload, "action": action})
        assert command.acknowledge_unresolved_conditions is False
    with pytest.raises(ValidationError):
        DecisionCommand.model_validate({**payload, "action": "approve"})


@pytest.mark.parametrize("action", ["approve", "reject", "expire"])
def test_content_preserves_all_original_constraints_without_mutation(original_records, action):
    request, plan = original_records
    before = request.model_dump(), plan.model_dump()
    content = build_content(request, plan, source_content_sha256="c" * 64, action=action)
    assert content.request_id == request.id
    assert content.plan_id == plan.id
    assert content.plan_content_sha256 == plan.content_sha256
    assert content.candidate_job_id == plan.candidate_job_id
    assert content.source_content_sha256 == "c" * 64
    assert content.evidence_ids == plan.content.evidence_ids
    assert content.preconditions == plan.content.preconditions
    assert content.unresolved_questions == plan.content.unresolved_questions
    assert content.outcome == target(action)[0]
    assert content.execution_eligible is False
    assert before == (request.model_dump(), plan.model_dump())
    assert "plan" not in content.model_dump()


@pytest.mark.parametrize(
    "field,value",
    [
        ("plan_id", uuid4()),
        ("investigation_run_id", uuid4()),
        ("candidate_job_id", uuid4()),
        ("plan_content_sha256", "0" * 64),
        ("wait_version", 2),
        ("previous_incident_version", 1),
    ],
)
def test_content_rejects_cross_request_plan_bindings(original_records, field, value):
    request, plan = original_records
    with pytest.raises(DecisionError, match="不一致"):
        build_content(
            request.model_copy(update={field: value}),
            plan,
            source_content_sha256="c" * 64,
            action="approve",
        )


def test_content_detects_erased_constraints_even_when_record_hash_was_not_changed(original_records):
    request, plan = original_records
    forged_content = plan.content.model_copy(update={"unresolved_questions": ()})
    with pytest.raises(DecisionError):
        build_content(
            request,
            plan.model_copy(update={"content": forged_content}),
            source_content_sha256="c" * 64,
            action="approve",
        )


def test_content_contract_cannot_accept_execution_eligibility(original_records):
    request, plan = original_records
    content = build_content(request, plan, source_content_sha256="c" * 64, action="approve")
    with pytest.raises(ValidationError):
        DecisionContent.model_validate({**content.model_dump(), "execution_eligible": True})


def transition(request, command=None, **changes):
    validate_transition(
        command or DecisionCommand.model_validate(command_data(request)),
        request,
        **{
            "availability": "current",
            "incident_status": "awaiting_approval",
            "incident_version": request.incident_version,
            "now": NOW,
            **changes,
        },
    )


@pytest.mark.parametrize(
    "action,availability,now",
    [
        ("approve", "current", NOW),
        ("reject", "current", NOW),
        ("reject", "stale", NOW),
        ("expire", "expired", NOW + timedelta(minutes=5)),
        ("expire", "stale", NOW + timedelta(minutes=6)),
    ],
)
def test_actions_accept_only_their_own_phase_and_deadline(
    original_records, action, availability, now
):
    request, _ = original_records
    command = DecisionCommand.model_validate(
        command_data(request, action=action, acknowledge_unresolved_conditions=action == "approve")
    )
    transition(request, command, availability=availability, now=now)


@pytest.mark.parametrize("action", ["approve", "reject", "expire"])
def test_every_action_requires_the_original_request_incident_version(original_records, action):
    request, _ = original_records
    command = DecisionCommand.model_validate(
        command_data(
            request,
            action=action,
            acknowledge_unresolved_conditions=action == "approve",
            expected_incident_version=4,
        )
    )
    with pytest.raises(DecisionError) as caught:
        transition(request, command, incident_version=4, now=request.expires_at)
    assert caught.value.code == "candidate_decision_incident_version_conflict"


@pytest.mark.parametrize(
    "changes,code",
    [
        ({"request_id": uuid4()}, "candidate_decision_request_mismatch"),
        ({"plan_content_sha256": "0" * 64}, "candidate_decision_plan_mismatch"),
    ],
)
def test_command_cannot_select_another_request_or_plan(original_records, changes, code):
    request, _ = original_records
    with pytest.raises(DecisionError) as caught:
        transition(request, DecisionCommand.model_validate(command_data(request, **changes)))
    assert caught.value.code == code


@pytest.mark.parametrize("status", ["investigating", "mitigating", None])
def test_changed_incident_stage_is_never_reused(original_records, status):
    request, _ = original_records
    with pytest.raises(DecisionError):
        transition(request, incident_status=status)


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_deadline_equality_needs_explicit_expire_and_never_auto_converts(original_records, action):
    request, _ = original_records
    command = DecisionCommand.model_validate(
        command_data(request, action=action, acknowledge_unresolved_conditions=action == "approve")
    )
    with pytest.raises(DecisionError) as caught:
        transition(request, command, now=request.expires_at)
    assert caught.value.code == "candidate_decision_expired"


def test_expire_before_deadline_is_rejected(original_records):
    request, _ = original_records
    command = DecisionCommand.model_validate(
        command_data(request, action="expire", acknowledge_unresolved_conditions=False)
    )
    with pytest.raises(DecisionError) as caught:
        transition(request, command, now=request.expires_at - timedelta(microseconds=1))
    assert caught.value.code == "candidate_decision_not_expired"


def test_stale_source_cannot_be_approved(original_records):
    request, _ = original_records
    with pytest.raises(DecisionError) as caught:
        transition(request, availability="stale")
    assert caught.value.code == "candidate_decision_source_stale"


def decision_record(request, plan, action="approve"):
    content = build_content(request, plan, source_content_sha256="c" * 64, action=action)
    return DecisionRecord(
        id=uuid4(),
        investigation_run_id=request.investigation_run_id,
        candidate_job_id=request.candidate_job_id,
        request_id=request.id,
        plan_content_sha256=request.plan_content_sha256,
        previous_incident_version=request.incident_version,
        incident_version=request.incident_version + 1,
        event_cursor=6,
        outcome=content.outcome,
        policy_version=POLICY_VERSION,
        decided_by_id="approver",
        authorization_id=uuid4(),
        idempotency_key="decision-test-1",
        command_sha256="d" * 64,
        content=content,
        content_sha256=content_hash(content),
        created_at=NOW,
    )


def availability(record, **changes):
    return decision_availability(
        record,
        **{
            "source_kind": "terminal_handoff",
            "run_status": "await_candidate",
            "latest_wait_version": None,
            "wait_cancelled": False,
            "incident_status": "mitigating",
            "incident_version": record.incident_version,
            "expires_at": NOW + timedelta(minutes=5),
            "now": NOW,
            **changes,
        },
    )


def test_legitimate_approval_uses_its_new_phase_instead_of_pending_request(original_records):
    record = decision_record(*original_records)
    assert availability(record) == "current"
    assert availability(record, incident_status="awaiting_approval", incident_version=3) == "stale"
    assert record.content.execution_eligible is False


def test_human_approval_tracks_original_wait_version_without_losing_constraints(original_records):
    request, plan = original_records
    record = decision_record(
        request.model_copy(update={"wait_version": 2}), plan.model_copy(update={"wait_version": 2})
    )
    source = {"source_kind": "human_route", "run_status": "await_human"}
    assert availability(record, **source, latest_wait_version=2) == "current"
    assert availability(record, **source, latest_wait_version=3) == "stale"
    assert availability(record, **source, latest_wait_version=2, wait_cancelled=True) == "stale"


@pytest.mark.parametrize(
    "changes",
    [
        {"run_status": "running"},
        {"run_status": "cancelled"},
        {"incident_status": "investigating"},
        {"incident_version": 5},
        {"incident_status": None, "incident_version": None},
    ],
)
def test_changed_source_or_incident_preserves_immutable_approval_as_stale(
    original_records, changes
):
    record = decision_record(*original_records)
    before = record.model_dump()
    assert availability(record, **changes) == "stale"
    assert record.model_dump() == before


def test_approval_expiry_takes_precedence_over_source_staleness(original_records):
    record = decision_record(*original_records)
    assert availability(record, now=NOW + timedelta(minutes=5), run_status="cancelled") == "expired"


@pytest.mark.parametrize("action", ["reject", "expire"])
def test_negative_decision_remains_recorded_after_future_source_and_deadline_changes(
    original_records, action
):
    record = decision_record(*original_records, action=action)
    assert (
        availability(
            record,
            run_status="cancelled",
            incident_status="closed",
            incident_version=99,
            now=NOW + timedelta(days=1),
        )
        == "recorded"
    )
    assert record.content.execution_eligible is False


def test_timezone_normalization_and_naive_time_rejection(original_records):
    request, plan = original_records
    transition(request, now=NOW.astimezone(timezone(timedelta(hours=8))))
    with pytest.raises(ValueError, match="时区"):
        transition(request, now=NOW.replace(tzinfo=None))
    record = decision_record(request, plan)
    with pytest.raises(ValueError, match="时区"):
        availability(record, expires_at=NOW.replace(tzinfo=None))


def test_action_mapping_keeps_negative_decisions_in_investigation():
    assert target("approve") == ("approved", IncidentAction.APPROVE_PLAN, "mitigating")
    assert target("reject") == ("rejected", IncidentAction.REJECT_PLAN, "investigating")
    assert target("expire") == ("expired", IncidentAction.EXPIRE_APPROVAL, "investigating")


@pytest.mark.parametrize("action", ["approve", "reject", "expire"])
def test_postwrite_timing_does_not_require_incident_to_remain_pending(action):
    if action == "expire":
        validate_timing(action, expires_at=NOW, now=NOW)
        with pytest.raises(DecisionError):
            validate_timing(action, expires_at=NOW, now=NOW - timedelta(microseconds=1))
    else:
        validate_timing(action, expires_at=NOW, now=NOW - timedelta(microseconds=1))
        with pytest.raises(DecisionError):
            validate_timing(action, expires_at=NOW, now=NOW)
