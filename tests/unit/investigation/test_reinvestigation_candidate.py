"""回流候选精确来源、新证据闭包与原预算的纯规则验收。"""

import json
from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallOperation
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.store import CallBudgetStore
from opsswarm.investigation.candidate.context import CAPABILITY_CATALOG
from opsswarm.investigation.candidate.output import CandidateModelResult, CandidateProposal
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.reinvestigation_candidate.context import PROMPT, build_packet
from opsswarm.investigation.reinvestigation_candidate.contracts import (
    ReflowCandidateError,
    ReflowCandidateSource,
)
from opsswarm.investigation.reinvestigation_candidate.policy import (
    check_budget,
    evaluate_candidate,
    validate_proposal,
)
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.planning.models import ActionType
from tests.unit.investigation import test_reinvestigation_dispatch_policy as dispatch
from tests.unit.investigation import test_reinvestigation_execution_materials as execution
from tests.unit.investigation.test_reflow_context_policy import budget_snapshot, model_call
from tests.unit.investigation.test_reinvestigation_routing import (
    compile_for,
    material_source,
    result_for,
)


def source_fixture(*, extra_evidence=False, **changes):
    original_result, original_observation = dispatch.coordination_result, execution.observation
    original_snapshot = execution.TaskEvidenceSnapshot

    def snapshot(**kwargs):
        value = original_snapshot(**kwargs)
        if extra_evidence:
            second = value.evidence[0].model_copy(update={"evidence_id": "new-change-extra"})
            value = value.model_copy(update={"evidence": value.evidence + (second,)})
        return value

    def coordination(source, **kwargs):
        result = original_result(source, count=3, **kwargs)
        task = next(t for t in result.proposal.tasks if t.role.value == "change")
        return result.model_copy(
            update={"proposal": result.proposal.model_copy(update={"tasks": (task,)})}
        )

    def observation(assignment, window, **kwargs):
        item = original_observation(assignment, window, **kwargs)
        return item.model_copy(
            update={
                "attributes": item.attributes
                | {
                    "change_type": "deployment",
                    "completed": True,
                    "from_version": "v1",
                    "to_version": "v2",
                }
            }
        )

    with (
        patch.object(dispatch, "coordination_result", coordination),
        patch.object(execution, "observation", observation),
        patch.object(execution, "TaskEvidenceSnapshot", snapshot),
    ):
        prior = material_source(**changes)
    review = result_for(prior)
    routing = compile_for(prior, review=review)
    return ReflowCandidateSource(
        investigation_run_id=prior.investigation_run_id,
        incident_id=prior.incident_id,
        parent_attempt_id=prior.parent_attempt_id,
        call_budget=prior.call_budget,
        model_budget=prior.model_budget,
        started_at=prior.started_at,
        created_at=routing.created_at + timedelta(seconds=1),
        service_name="order-service",
        review_source=prior,
        review_result=review,
        routing=routing,
        routing_sha256=content_hash(routing),
    )


def proposal(source, **changes):
    return CandidateProposal(
        **(
            {
                "status": "candidate",
                "action_type": ActionType.ROLLBACK_RELEASE,
                "rationale": "只评估新窗口变更的回滚模板，因果仍待核实",
                "evidence_ids": source.review_result.proposal.accepted_supporting_evidence_ids,
                "preconditions": ("仍需新计划、现场核验和可信审批",),
                "unresolved_questions": (
                    source.review_source.synthesis_result.proposal.unresolved_questions
                ),
            }
            | changes
        )
    )


def test_full_new_basis_unresolved_questions_and_windows_enter_context():
    source = source_fixture()
    packet = build_packet(source)
    payload = json.loads(packet.model_input.messages[1].content)
    assert payload == {
        "source": source.model_dump(mode="json"),
        "capability_catalog": list(CAPABILITY_CATALOG),
    }
    assert packet.model_input.messages[0].content == PROMPT
    assert packet.manifest.routing_sha256 == content_hash(source.routing)
    assert packet.manifest.tool_names == packet.manifest.trimming == ()
    assert source.routing.unresolved_questions
    assert (
        source.routing.observation_windows[0].effective_window
        == source.review_source.professionals[0].effective_window
    )


@pytest.mark.parametrize(
    "field,value",
    [
        ("routing_sha256", "0" * 64),
        ("parent_attempt_id", uuid4()),
        ("investigation_run_id", uuid4()),
    ],
)
def test_wrong_root_or_routing_binding_rejected(field, value):
    with pytest.raises(ReflowCandidateError):
        build_packet(source_fixture().model_copy(update={field: value}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("source_call_id", uuid4()),
        ("review_result_sha256", "0" * 64),
        ("unresolved_questions", ("被替换的问题",)),
        ("observation_windows", ()),
        ("route", "await_human"),
    ],
)
def test_rehashed_wrong_route_body_still_rejected(field, value):
    source = source_fixture()
    route = source.routing.model_copy(update={field: value})
    source = source.model_copy(update={"routing": route, "routing_sha256": content_hash(route)})
    with pytest.raises(ReflowCandidateError):
        build_packet(source)


def test_unknown_hypothesis_can_only_create_non_authorizing_template_evaluation():
    source = source_fixture()
    result = evaluate_candidate(source, CandidateModelResult(proposal=proposal(source)))
    assert result.disposition == "planning_review"
    assert result.execution_eligible is False
    assert result.step.parameters.target_version == "v1"
    assert result.step.requires_human_approval is True
    assert "root_cause" not in result.model_dump()
    assert source.review_source.synthesis_result.review_status == "pending_review"
    assert result.proposal.unresolved_questions == source.routing.unresolved_questions


@pytest.mark.parametrize("evidence_id", ["historical-only", "invented-new-proof"])
def test_old_or_foreign_evidence_cannot_support_candidate(evidence_id):
    source = source_fixture()
    with pytest.raises(ReflowCandidateError):
        validate_proposal(source, proposal(source, evidence_ids=(evidence_id,)))


def test_same_round_evidence_without_accepted_support_is_insufficient():
    source = source_fixture(extra_evidence=True)
    assert "new-change-extra" in {
        e.evidence_id for p in source.review_source.professionals for e in p.evidence.evidence
    }
    with pytest.raises(ReflowCandidateError) as caught:
        validate_proposal(source, proposal(source, evidence_ids=("new-change-extra",)))
    assert caught.value.code == "candidate_support_missing"


def test_unsupported_preserves_non_authorizing_human_result():
    source = source_fixture()
    result = evaluate_candidate(
        source,
        CandidateModelResult(
            proposal=proposal(
                source,
                status="unsupported",
                action_type=None,
                evidence_ids=(),
                preconditions=(),
            )
        ),
    )
    assert result.disposition == "await_human"
    assert result.step is None and result.execution_eligible is False


def test_unknown_allocation_counts_and_cannot_reset_original_budget():
    source = source_fixture(call_budget=CallBudgetPolicy(max_model_calls=1))
    packet = build_packet(source)
    call = model_call(
        source,
        packet,
        operation=CallOperation.EVALUATE_REINVESTIGATION_CANDIDATE,
        reinvestigation_attempt_id=None,
        reflow_candidate_attempt_id=uuid4(),
    )
    budget = budget_snapshot(source, (call,))
    assert budget.model_usage.unknown_model_calls == 1
    with pytest.raises(CallBudgetExceeded) as caught:
        check_budget(source, packet, budget, now=source.created_at, allocate=True)
    assert caught.value.code == "model_call_budget_exceeded"
    with pytest.raises(ReflowCandidateError):
        check_budget(
            source,
            packet,
            budget.model_copy(update={"reserved_model_calls": 0}),
            now=source.created_at,
            allocate=False,
        )


def test_only_own_candidate_reservation_may_remain_inflight():
    source = source_fixture()
    packet = build_packet(source)
    call = model_call(
        source,
        packet,
        status="reserved",
        operation=CallOperation.EVALUATE_REINVESTIGATION_CANDIDATE,
        reinvestigation_attempt_id=None,
        reflow_candidate_attempt_id=uuid4(),
    )
    budget = budget_snapshot(source, (call,))
    check_budget(source, packet, budget, now=source.created_at, allocate=False, own_call_id=call.id)
    foreign = call.model_copy(
        update={
            "operation": CallOperation.REVIEW_REINVESTIGATION,
            "reflow_candidate_attempt_id": None,
            "reflow_review_attempt_id": uuid4(),
        }
    )
    with pytest.raises(CallBudgetExceeded):
        check_budget(
            source,
            packet,
            budget_snapshot(source, (foreign,)),
            now=source.created_at,
            allocate=False,
            own_call_id=foreign.id,
        )


def test_original_deadline_is_not_restarted_for_candidate():
    source = source_fixture()
    packet = build_packet(source)
    with pytest.raises(CallBudgetExceeded) as caught:
        check_budget(
            source,
            packet,
            budget_snapshot(source),
            now=source.started_at + timedelta(seconds=source.call_budget.max_duration_seconds),
            allocate=True,
        )
    assert caught.value.code == "run_deadline_exceeded"


@pytest.mark.asyncio
async def test_parent_budget_entry_cannot_reserve_candidate_stage():
    def forbidden_factory():
        raise AssertionError("独立阶段应在访问父事务前拒绝")

    with pytest.raises(TaskFactConflict) as caught:
        await CallBudgetStore(forbidden_factory).reserve(
            uuid4(), lease_token=uuid4(), operation=CallOperation.EVALUATE_REINVESTIGATION_CANDIDATE
        )
    assert caught.value.code == "call_scope_invalid"
