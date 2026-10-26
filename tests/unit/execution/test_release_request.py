"""候选发布准备的精确批准、有限CAS及只读观测时限。"""

from datetime import UTC, datetime, timedelta, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.approval.conditions.contracts import DecisionRecord
from opsswarm.approval.conditions.policy import build_decision_content
from opsswarm.execution.release.contracts import (
    POLICY_VERSION,
    ReleaseObservation,
    ReleaseRequestAuditRecord,
    ReleaseRequestCommand,
    ReleaseRequestContent,
    ReleaseRequestError,
    ReleaseRequestRecord,
    ReleaseRequestView,
)
from opsswarm.execution.release.policy import (
    build_content,
    expires_at,
    validate_command,
    validate_release_plan,
)
from opsswarm.fault_lab.release.contracts import ReleaseState
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.planning.models import ActionPlan, ActionType, RiskLevel
from tests.unit.approval.test_condition_approval import request_for
from tests.unit.execution.test_candidate_conditions import NOW, decision_for, plan_for, record_for
from tests.unit.execution.test_candidate_preflight import step_for


def sources_for():
    plan = plan_for(preconditions=("核实发布回滚前提",), questions=("确认目标稳定版本",))
    step = step_for(ActionType.ROLLBACK_RELEASE)
    inner = ActionPlan.model_validate(
        {
            **plan.content.plan.model_dump(),
            "steps": (step,),
            "risk_level": step.risk_level,
            "requires_human_approval": step.requires_human_approval,
        }
    )
    content = plan.content.model_copy(update={"plan": inner})
    plan = plan.model_copy(update={"content": content, "content_sha256": content_hash(content)})
    verification = record_for(plan, decision_for(plan))
    request = request_for(verification)
    approved_content = build_decision_content(request, "approve")
    approval = DecisionRecord(
        id=uuid4(),
        investigation_run_id=plan.investigation_run_id,
        candidate_job_id=plan.candidate_job_id,
        request_id=request.id,
        request_content_sha256=request.content_sha256,
        verification_id=verification.id,
        verification_content_sha256=verification.content_sha256,
        verification_version=verification.version,
        incident_version=verification.incident_version,
        policy_version="condition-approval-v1",
        outcome="approved",
        decided_by_id="trusted-approver",
        authorization_id=uuid4(),
        idempotency_key="release-approved-unit",
        command_sha256="b" * 64,
        content=approved_content,
        content_sha256=content_hash(approved_content),
        created_at=NOW,
    )
    return plan, approval


def observation_data():
    return dict(
        state=dict(
            service_name="order-service",
            source="local-release-simulation-v1",
            simulation=True,
            version="2026.01.01-rc1",
            revision=4,
            command_count=5,
            release_500_enabled=True,
            updated_at=NOW - timedelta(seconds=3),
        ),
        observed_at=NOW,
        source_path="/__admin/simulated-release",
    )


def observation_for():
    return ReleaseObservation.model_validate(observation_data())


def command_for(approval, **changes):
    return ReleaseRequestCommand.model_validate(
        {
            "approval_decision_id": approval.id,
            "approval_content_sha256": approval.content_sha256,
            "expected_incident_version": approval.incident_version,
            "idempotency_key": "prepare-release-unit",
            "reason": "核对精确批准并准备只读CAS",
            **changes,
        }
    )


def test_content_binds_complete_approval_and_unique_operation_without_granting_dispatch():
    plan, approval = sources_for()
    observation = observation_for()
    before_plan, before_approval = plan.model_dump(), approval.model_dump()
    operation = uuid4()
    content = build_content(approval, plan, observation, operation)
    assert content.version == "candidate-release-execution-request-content-v1"
    assert (
        content.approval_decision_id == approval.id
        and content.approval_content_sha256 == approval.content_sha256
    )
    assert content.verification_id == approval.verification_id
    assert content.verification_version == approval.verification_version
    assert content.verification_content_sha256 == approval.verification_content_sha256
    assert content.plan_id == plan.id and content.plan_content_sha256 == plan.content_sha256
    assert content.step == plan.content.plan.steps[0] and content.observation == observation
    assert content.operation_id == operation and content.dispatch_eligible is False
    assert content.cas_command.model_dump() == dict(
        expected_version="2026.01.01-rc1",
        expected_revision=4,
        target_version="stable",
        idempotency_key=f"candidate-release-{operation}",
    )
    assert plan.model_dump() == before_plan and approval.model_dump() == before_approval
    assert approval.content.execution_eligible is False and plan.content.execution_eligible is False


@pytest.mark.parametrize("value", [1, 0, "true", False, None])
def test_observation_requires_explicit_real_boolean_simulation(value):
    raw = observation_data()
    raw["state"]["simulation"] = value
    with pytest.raises(ValidationError):
        ReleaseObservation.model_validate(raw)
    forged = ReleaseState.model_construct(**raw["state"])
    with pytest.raises(ValidationError):
        ReleaseObservation(state=forged, observed_at=NOW)


@pytest.mark.parametrize("field", list(observation_data()["state"]))
def test_observation_does_not_fill_missing_remote_source_fields(field):
    raw = observation_data()
    raw["state"].pop(field)
    with pytest.raises(ValidationError):
        ReleaseObservation.model_validate(raw)


@pytest.mark.parametrize(
    "field,value",
    [
        ("service_name", "elsewhere"),
        ("source", "deployment-environment"),
        ("version", "latest"),
        ("revision", True),
        ("revision", 4.0),
        ("revision", "4"),
        ("revision", 7),
        ("command_count", False),
        ("command_count", 5.0),
        ("release_500_enabled", 1),
        ("release_500_enabled", False),
        ("updated_at", 123),
        ("updated_at", NOW + timedelta(seconds=1)),
        ("updated_at", NOW.replace(tzinfo=None)),
    ],
)
def test_observation_refuses_incomplete_counter_or_source_identity(field, value):
    raw = observation_data()
    raw["state"][field] = value
    with pytest.raises(ValidationError):
        ReleaseObservation.model_validate(raw)


@pytest.mark.parametrize("value", [123, "123", NOW.replace(tzinfo=None), "2026-01-01T00:00:00"])
def test_observed_at_rejects_epoch_and_missing_timezone(value):
    raw = observation_data()
    raw["observed_at"] = value
    with pytest.raises(ValidationError):
        ReleaseObservation.model_validate(raw)


def test_observation_normalizes_both_remote_and_collection_times_to_utc():
    raw = observation_data()
    offset = timezone(timedelta(hours=8))
    raw["state"]["updated_at"] = raw["state"]["updated_at"].astimezone(offset)
    raw["observed_at"] = NOW.astimezone(offset)
    observation = ReleaseObservation.model_validate(raw)
    assert observation.observed_at.tzinfo is UTC and observation.state.updated_at.tzinfo is UTC
    assert observation.observed_at == NOW


@pytest.mark.parametrize(
    "changes",
    [
        {"outcome": "rejected"},
        {"content_sha256": "f" * 64},
        {"request_id": uuid4()},
        {"request_content_sha256": "f" * 64},
        {"verification_id": uuid4()},
        {"verification_version": 2},
        {"verification_content_sha256": "f" * 64},
    ],
)
def test_preparation_refuses_nonapproved_or_inconsistent_approval(changes):
    plan, approval = sources_for()
    with pytest.raises(ReleaseRequestError):
        build_content(approval.model_copy(update=changes), plan, observation_for(), uuid4())


@pytest.mark.parametrize(
    "changes",
    [
        {"id": uuid4()},
        {"content_sha256": "f" * 64},
        {"investigation_run_id": uuid4()},
        {"candidate_job_id": uuid4()},
    ],
)
def test_preparation_requires_the_exact_plan_and_ownership(changes):
    plan, approval = sources_for()
    with pytest.raises(ReleaseRequestError):
        build_content(approval, plan.model_copy(update=changes), observation_for(), uuid4())


@pytest.mark.parametrize(
    "action", [ActionType.ROLLBACK_CONFIGURATION, ActionType.RESTORE_DEPENDENCY]
)
def test_nonrelease_steps_are_rejected_before_network_observation(action):
    plan, _ = sources_for()
    step = step_for(action)
    inner = plan.content.plan.model_copy(
        update={
            "steps": (step,),
            "risk_level": step.risk_level,
            "requires_human_approval": step.requires_human_approval,
        }
    )
    content = plan.content.model_copy(update={"plan": inner})
    plan = plan.model_copy(update={"content": content, "content_sha256": content_hash(content)})
    with pytest.raises(ReleaseRequestError):
        validate_release_plan(plan)


@pytest.mark.parametrize(
    "field,value",
    [
        ("target_name", "billing-service"),
        ("risk_level", RiskLevel.LOW),
        ("requires_human_approval", False),
    ],
)
def test_modified_targets_and_risk_cannot_pass_the_plan_gate(field, value):
    plan, _ = sources_for()
    step = plan.content.plan.steps[0].model_copy(update={field: value})
    inner = plan.content.plan.model_copy(update={"steps": (step,)})
    content = plan.content.model_copy(update={"plan": inner})
    plan = plan.model_copy(update={"content": content, "content_sha256": content_hash(content)})
    with pytest.raises(ReleaseRequestError):
        validate_release_plan(plan)


def test_already_stable_observation_does_not_become_noop_execution_preparation():
    plan, approval = sources_for()
    raw = observation_data()
    raw["state"].update(version="stable", release_500_enabled=False)
    with pytest.raises(ReleaseRequestError, match="原计划rc1"):
        build_content(approval, plan, ReleaseObservation.model_validate(raw), uuid4())


@pytest.mark.parametrize("availability", ["pending", "superseded", "stale", "expired"])
def test_command_requires_a_current_approval(availability):
    _, approval = sources_for()
    with pytest.raises(ReleaseRequestError):
        validate_command(
            command_for(approval),
            approval,
            availability=availability,
            now=NOW,
            expires_at=NOW + timedelta(seconds=300),
        )


@pytest.mark.parametrize(
    "changes",
    [
        {"approval_decision_id": uuid4()},
        {"approval_content_sha256": "f" * 64},
        {"expected_incident_version": 99},
    ],
)
def test_command_binds_exact_approved_content_and_incident(changes):
    _, approval = sources_for()
    with pytest.raises(ReleaseRequestError):
        validate_command(
            command_for(approval, **changes),
            approval,
            availability="current",
            now=NOW,
            expires_at=NOW + timedelta(seconds=300),
        )


@pytest.mark.parametrize("value", [True, 4.0, "4", 1])
def test_command_requires_strict_incident_version(value):
    _, approval = sources_for()
    with pytest.raises(ValidationError):
        command_for(approval, expected_incident_version=value)


@pytest.mark.parametrize("offset,allowed", [(-1, False), (0, True), (299, True), (300, False)])
def test_command_checks_original_approval_creation_and_deadline(offset, allowed):
    _, approval = sources_for()
    args = dict(
        availability="current",
        now=NOW + timedelta(seconds=offset),
        expires_at=NOW + timedelta(seconds=300),
    )
    if allowed:
        validate_command(command_for(approval), approval, **args)
    else:
        with pytest.raises(ReleaseRequestError):
            validate_command(command_for(approval), approval, **args)


@pytest.mark.parametrize("seconds", [0, 1, 29])
def test_fresh_observation_expiry_is_clamped_to_original_deadline(seconds):
    observation = observation_for()
    assert expires_at(
        NOW + timedelta(seconds=300), observation, NOW + timedelta(seconds=seconds)
    ) == NOW + timedelta(seconds=30)
    assert expires_at(
        NOW + timedelta(seconds=seconds + 1), observation, NOW + timedelta(seconds=seconds)
    ) == NOW + timedelta(seconds=seconds + 1)


@pytest.mark.parametrize("offset", [-1, 30, 31])
def test_future_or_expired_observation_cannot_prepare(offset):
    with pytest.raises(ReleaseRequestError):
        expires_at(NOW + timedelta(seconds=300), observation_for(), NOW + timedelta(seconds=offset))


def test_deadline_and_utc_overflow_are_not_extended_or_silently_normalized():
    observation = observation_for()
    with pytest.raises(ReleaseRequestError):
        expires_at(NOW, observation, NOW)
    with pytest.raises(ValueError):
        expires_at(NOW.replace(tzinfo=None), observation, NOW)
    last = datetime(9999, 12, 31, 23, 59, 50, tzinfo=UTC)
    raw = observation_data()
    raw["observed_at"] = raw["state"]["updated_at"] = last
    with pytest.raises(ReleaseRequestError):
        expires_at(last, ReleaseObservation.model_validate(raw), last)


@pytest.mark.parametrize("value", [True, 0, 1, "false", None])
def test_content_never_accepts_an_enabled_or_coerced_dispatch_flag(value):
    plan, approval = sources_for()
    content = build_content(approval, plan, observation_for(), uuid4())
    with pytest.raises(ValidationError):
        ReleaseRequestContent.model_validate({**content.model_dump(), "dispatch_eligible": value})


@pytest.mark.parametrize("value", [False, 0, 1, "true", None])
def test_view_always_requires_a_fresh_dispatch_check(value):
    with pytest.raises(ValidationError):
        ReleaseRequestView(record=None, availability="pending", requires_dispatch_recheck=value)


def test_audit_outcome_cannot_mix_error_fields_with_success_or_forge_http_status():
    _, approval = sources_for()
    command = command_for(approval)
    audit = dict(
        cursor=1,
        authorization_id=uuid4(),
        investigation_run_id=approval.investigation_run_id,
        candidate_job_id=approval.candidate_job_id,
        verification_version=1,
        subject_id="executor",
        command=command,
        command_sha256=content_hash(command),
        outcome="applied",
        http_status=201,
        request_id=uuid4(),
        error_code=None,
        error_message=None,
        created_at=NOW,
    )
    assert ReleaseRequestAuditRecord.model_validate(audit).outcome == "applied"
    for changes in (
        {"http_status": 200},
        {"request_id": None},
        {"error_code": "forged"},
        {"error_message": "伪造错误"},
        {"outcome": "replayed"},
    ):
        with pytest.raises(ValidationError):
            ReleaseRequestAuditRecord.model_validate({**audit, **changes})
    error = {
        **audit,
        "outcome": "error",
        "http_status": 409,
        "request_id": None,
        "error_code": "conflict",
        "error_message": "准备冲突",
    }
    assert ReleaseRequestAuditRecord.model_validate(error).request_id is None


def test_record_matches_prepared_orm_shape_without_inventing_mutable_source_columns():
    plan, approval = sources_for()
    content = build_content(approval, plan, observation_for(), uuid4())
    record = ReleaseRequestRecord(
        id=uuid4(),
        operation_id=content.operation_id,
        investigation_run_id=approval.investigation_run_id,
        candidate_job_id=approval.candidate_job_id,
        approval_decision_id=approval.id,
        approval_content_sha256=approval.content_sha256,
        verification_version=approval.verification_version,
        incident_version=approval.incident_version,
        policy_version=POLICY_VERSION,
        requested_by_id="executor",
        authorization_id=uuid4(),
        idempotency_key="prepare-release-unit",
        command_sha256="c" * 64,
        content=content,
        content_sha256=content_hash(content),
        created_at=NOW,
        expires_at=NOW + timedelta(seconds=30),
    )
    assert record.status == "prepared" and record.content.dispatch_eligible is False
    assert (
        "verification_id" not in record.model_dump()
        and "verification_content_sha256" not in record.model_dump()
    )
