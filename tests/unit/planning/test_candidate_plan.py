"""候选规划只消费可重算来源，保留开放假设并拒绝资格提升。"""

from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.investigation.candidate.evaluation import evaluate_candidate
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.models import EvidenceKind
from opsswarm.planning.candidate.contracts import PlanCommand, PlanContent, PlanError
from opsswarm.planning.candidate.policy import PLAN_SUMMARY, build_plan
from opsswarm.planning.models import ActionPlanStatus, ActionType, RiskLevel
from tests.unit.investigation.test_candidate_evaluation import (
    candidate,
    candidate_source,
    observation,
)


def command_data(**changes):
    return {
        "candidate_result_sha256": "a" * 64,
        "expected_wait_version": None,
        "expected_incident_version": 1,
        "idempotency_key": "plan-test-1",
        "reason": "明确请求生成尚未授权的候选草案",
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
        {"candidate_result_sha256": "A" * 64},
        {"idempotency_key": "short"},
        {"reason": " "},
        {"action": "approve"},
        {"parameters": {"target_version": "v1"}},
        {"execution_eligible": True},
    ],
)
def test_command_rejects_coerced_versions_and_authority_fields(changes):
    with pytest.raises(ValidationError):
        PlanCommand.model_validate(command_data(**changes))


def test_wait_version_is_required_but_accepts_explicit_null():
    data = command_data()
    assert PlanCommand.model_validate(data).expected_wait_version is None
    assert PlanCommand.model_validate(command_data(expected_wait_version=2)).action == "create_plan"
    del data["expected_wait_version"]
    with pytest.raises(ValidationError):
        PlanCommand.model_validate(data)


@pytest.mark.parametrize(
    "action_type,kind,attributes,signal",
    [
        (
            ActionType.ROLLBACK_RELEASE,
            EvidenceKind.CHANGE,
            {
                "change_type": "deployment",
                "completed": True,
                "from_version": "v1",
                "to_version": "v2",
            },
            "http_5xx_ratio",
        ),
        (
            ActionType.ROLLBACK_CONFIGURATION,
            EvidenceKind.CHANGE,
            {
                "change_type": "configuration",
                "completed": True,
                "key": "database_pool_size",
                "from_value": 40,
                "to_value": 10,
            },
            "database_pool_saturation",
        ),
        (
            ActionType.RESTORE_DEPENDENCY,
            EvidenceKind.METRIC,
            {"redis_healthy": False},
            "redis_unavailable_ratio",
        ),
    ],
)
def test_three_capabilities_preserve_open_hypothesis_conditions_and_safe_step(
    action_type, kind, attributes, signal
):
    source = candidate_source().model_copy(
        update={"evidence": (observation(kind=kind, attributes=attributes),)}
    )
    result = evaluate_candidate(
        source,
        candidate(
            action_type=action_type,
            preconditions=("先核实现场条件", "人工决定是否继续审批"),
            unresolved_questions=("机制尚未证明",),
        ),
    )
    job_id = uuid4()
    original = source.model_dump(mode="json"), result.model_dump(mode="json")
    content = build_plan(job_id, source, result, content_hash(result))
    assert content.candidate_job_id == job_id
    assert content.candidate_result_sha256 == content_hash(result)
    assert content.open_hypothesis_id == source.hypothesis.id
    assert content.evidence_ids == result.proposal.evidence_ids
    assert content.preconditions == result.proposal.preconditions
    assert content.unresolved_questions == result.proposal.unresolved_questions
    assert content.plan.status is ActionPlanStatus.DRAFT
    assert content.plan.version == 1
    assert content.plan.risk_policy_version == "action-risk-v3"
    assert content.plan.risk_level is RiskLevel.HIGH
    assert content.plan.requires_human_approval
    assert content.plan.summary == PLAN_SUMMARY
    assert content.plan.steps == (result.step,)
    assert content.plan.steps[0].verification.signal == signal
    assert content.approval_eligible is content.execution_eligible is False
    assert "root_cause" not in content.model_dump_json()
    assert original == (source.model_dump(mode="json"), result.model_dump(mode="json"))


def test_result_hash_is_checked_before_building_plan():
    source = candidate_source()
    result = evaluate_candidate(source, candidate())
    with pytest.raises(PlanError) as caught:
        build_plan(uuid4(), source, result, "0" * 64)
    assert caught.value.code == "candidate_plan_result_mismatch"


@pytest.mark.parametrize("unsupported", [True, False])
def test_unsupported_and_missing_parameters_cannot_become_plans(unsupported):
    source = candidate_source()
    proposal = candidate()
    if unsupported:
        proposal = candidate(
            status="unsupported", action_type=None, evidence_ids=(), preconditions=()
        )
    else:
        source = source.model_copy(update={"evidence": (observation(attributes={}),)})
    result = evaluate_candidate(source, proposal)
    with pytest.raises(PlanError) as caught:
        build_plan(uuid4(), source, result, content_hash(result))
    assert caught.value.code == "candidate_plan_not_ready"


def test_changed_source_and_forged_verification_are_rejected_even_with_matching_hash():
    source = candidate_source()
    result = evaluate_candidate(source, candidate())
    changed_source = source.model_copy(update={"evidence": (observation(attributes={}),)})
    with pytest.raises(PlanError, match="重算"):
        build_plan(uuid4(), changed_source, result, content_hash(result))
    assert result.step is not None
    forged_step = result.step.model_copy(
        update={"verification": result.step.verification.model_copy(update={"threshold": 1.0})}
    )
    forged = result.model_copy(update={"step": forged_step})
    with pytest.raises(PlanError, match="重算"):
        build_plan(uuid4(), source, forged, content_hash(forged))


def test_forged_evidence_reference_is_not_a_valid_source():
    source = candidate_source()
    result = evaluate_candidate(source, candidate())
    forged = result.model_copy(
        update={"proposal": result.proposal.model_copy(update={"evidence_ids": ("invented-1",)})}
    )
    with pytest.raises(PlanError) as caught:
        build_plan(uuid4(), source, forged, content_hash(forged))
    assert caught.value.code == "candidate_plan_source_invalid"


@pytest.mark.parametrize("field", ["approval_eligible", "execution_eligible"])
def test_content_cannot_acquire_approval_or_execution_eligibility(field):
    source = candidate_source()
    result = evaluate_candidate(source, candidate())
    content = build_plan(uuid4(), source, result, content_hash(result))
    with pytest.raises(ValidationError):
        PlanContent.model_validate({**content.model_dump(), field: True})


@pytest.mark.parametrize(
    "changes",
    [
        {"version": 2},
        {"status": "approved"},
        {"risk_policy_version": "action-risk-v1"},
    ],
)
def test_content_cannot_become_another_plan_version_or_approved_plan(changes):
    source = candidate_source()
    result = evaluate_candidate(source, candidate())
    content = build_plan(uuid4(), source, result, content_hash(result))
    payload = content.model_dump()
    payload["plan"].update(changes)
    with pytest.raises(ValidationError):
        PlanContent.model_validate(payload)
