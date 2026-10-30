"""新回流候选草案保留完整依据、不提升授权并共享原预算。"""

from datetime import timedelta
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallOperation
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.candidate.output import CandidateModelResult
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.reinvestigation_candidate.context import build_packet
from opsswarm.investigation.reinvestigation_candidate.contracts import ReflowCandidateResult
from opsswarm.investigation.reinvestigation_candidate.policy import evaluate_candidate
from opsswarm.planning.models import ActionPlanStatus
from opsswarm.planning.reinvestigation_candidate.budget import check_planning_budget
from opsswarm.planning.reinvestigation_candidate.contracts import (
    ReflowPlanCommand,
    ReflowPlanContent,
    ReflowPlanError,
)
from opsswarm.planning.reinvestigation_candidate.policy import build_plan
from tests.unit.investigation.test_reflow_context_policy import budget_snapshot, model_call
from tests.unit.investigation.test_reinvestigation_candidate import proposal, source_fixture


def result_fixture(source, **changes):
    return ReflowCandidateResult(
        investigation_run_id=source.investigation_run_id,
        job_id=source.investigation_run_id,
        source_call_id=uuid4(),
        source_sha256=content_hash(source),
        routing_sha256=source.routing_sha256,
        evaluation=evaluate_candidate(
            source, CandidateModelResult(proposal=proposal(source, **changes))
        ),
        created_at=source.created_at + timedelta(seconds=1),
    )


def command_data(**changes):
    return {
        "action": "create_plan",
        "candidate_result_sha256": "a" * 64,
        "routing_sha256": "b" * 64,
        "expected_incident_version": 2,
        "idempotency_key": "reflow-plan-command-139",
        "reason": "生成本次新依据下的待审草案",
    } | changes


@pytest.mark.parametrize(
    "changes",
    [
        {"expected_incident_version": True},
        {"expected_incident_version": "2"},
        {"expected_incident_version": 2.0},
        {"expected_incident_version": 0},
        {"candidate_result_sha256": "A" * 64},
        {"routing_sha256": "B" * 64},
        {"idempotency_key": "short"},
        {"reason": " "},
        {"action": "approve"},
        {"expected_wait_version": 2},
        {"parameters": {"target_version": "v1"}},
        {"credential_expires_at": "2099-01-01T00:00:00Z"},
    ],
)
def test_command_rejects_coerced_versions_and_client_authority(changes):
    with pytest.raises(ValidationError):
        ReflowPlanCommand.model_validate(command_data(**changes))


def test_plan_keeps_new_basis_and_safe_single_draft_without_old_authorization():
    source = source_fixture()
    result = result_fixture(source)
    before = source.model_dump(mode="json"), result.model_dump(mode="json")
    content = build_plan(source.investigation_run_id, source, result, content_hash(result))
    assert content.candidate_result_sha256 == content_hash(result)
    assert content.routing_sha256 == source.routing_sha256
    assert content.preconditions == result.evaluation.proposal.preconditions
    assert content.candidate_unresolved_questions == result.evaluation.proposal.unresolved_questions
    assert content.hypothesis == source.review_source.synthesis_result.proposal
    assert content.review_result_sha256 == source.routing.review_result_sha256
    assert content.synthesis_result_sha256 == source.routing.synthesis_result_sha256
    assert content.observation_windows == source.routing.observation_windows
    assert content.unresolved_constraints == source.routing.unresolved_constraints
    assert content.plan.status is ActionPlanStatus.DRAFT
    assert content.plan.version == 1 and len(content.plan.steps) == 1
    assert content.plan.steps == (result.evaluation.step,)
    assert content.plan.requires_human_approval
    assert content.approval_eligible is content.execution_eligible is False
    assert "root_cause" not in content.model_dump_json()
    assert before == (source.model_dump(mode="json"), result.model_dump(mode="json"))


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_sha256", "0" * 64),
        ("routing_sha256", "0" * 64),
        ("job_id", uuid4()),
        ("investigation_run_id", uuid4()),
    ],
)
def test_rehashed_foreign_candidate_result_cannot_become_plan(field, value):
    source = source_fixture()
    result = result_fixture(source).model_copy(update={field: value})
    with pytest.raises(ReflowPlanError):
        build_plan(source.investigation_run_id, source, result, content_hash(result))


def test_result_hash_and_recomputed_safe_step_are_required():
    source = source_fixture()
    result = result_fixture(source)
    with pytest.raises(ReflowPlanError):
        build_plan(source.investigation_run_id, source, result, "0" * 64)
    step = result.evaluation.step
    forged = step.model_copy(
        update={
            "verification": step.verification.model_copy(update={"threshold": 1.0}),
        }
    )
    result = result.model_copy(
        update={"evaluation": result.evaluation.model_copy(update={"step": forged})}
    )
    with pytest.raises(ReflowPlanError):
        build_plan(source.investigation_run_id, source, result, content_hash(result))


def test_unsupported_candidate_cannot_become_plan():
    source = source_fixture()
    result = result_fixture(
        source, status="unsupported", action_type=None, evidence_ids=(), preconditions=()
    )
    with pytest.raises(ReflowPlanError):
        build_plan(source.investigation_run_id, source, result, content_hash(result))


@pytest.mark.parametrize("field", ["approval_eligible", "execution_eligible"])
def test_plan_content_cannot_claim_approval_or_execution_eligibility(field):
    source = source_fixture()
    result = result_fixture(source)
    content = build_plan(source.investigation_run_id, source, result, content_hash(result))
    with pytest.raises(ValidationError):
        ReflowPlanContent.model_validate(content.model_dump(mode="json") | {field: True})


def budget_fixture(*, status="unknown", usage=None):
    source = source_fixture(call_budget=CallBudgetPolicy(max_model_calls=1))
    packet = build_packet(source)
    call = model_call(
        source,
        packet,
        status=status,
        operation=CallOperation.EVALUATE_REINVESTIGATION_CANDIDATE,
        reinvestigation_attempt_id=None,
        reflow_candidate_attempt_id=uuid4(),
        model_usage=usage,
    )
    return source, packet, budget_snapshot(source, (call,))


def test_exact_limit_and_unknown_usage_allow_no_new_reservation():
    source, packet, budget = budget_fixture()
    before = budget.model_dump(mode="json")
    check_planning_budget(source, packet, budget, now=source.created_at)
    assert budget.reserved_model_calls == source.call_budget.max_model_calls
    assert budget.model_usage.unknown_model_calls == 1
    assert budget.model_usage.reported_cost_usd is None
    assert budget.model_dump(mode="json") == before


def test_inflight_call_blocks_even_without_another_reservation():
    source, packet, budget = budget_fixture(status="reserved")
    with pytest.raises(ReflowPlanError):
        check_planning_budget(source, packet, budget, now=source.created_at)


def test_overreported_usage_and_wrong_count_do_not_gain_capacity():
    source, packet, budget = budget_fixture()
    with pytest.raises(ReflowPlanError):
        check_planning_budget(
            source,
            packet,
            budget.model_copy(update={"reserved_model_calls": 0}),
            now=source.created_at,
        )
    call = budget.calls[0]
    usage = ReportedModelUsage(
        input_tokens=packet.allocation.estimated_input_tokens + 1,
        output_tokens=0,
        total_tokens=packet.allocation.estimated_input_tokens + 1,
    )
    over = call.model_copy(update={"model_usage": usage})
    with pytest.raises(ReflowPlanError):
        check_planning_budget(
            source, packet, budget_snapshot(source, (over,)), now=source.created_at
        )


def test_plan_keeps_original_deadline():
    source, packet, budget = budget_fixture()
    with pytest.raises(ReflowPlanError) as caught:
        check_planning_budget(source, packet, budget, now=budget.deadline)
    assert caught.value.code == "run_deadline_exceeded"
