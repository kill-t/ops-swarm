"""候选纯规则保持开放假设、证据闭包与确定性参数边界。"""

import json
from datetime import timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.candidate.context import build_packet
from opsswarm.investigation.candidate.contracts import CandidateError, CandidateSource
from opsswarm.investigation.candidate.evaluation import evaluate_candidate, validate_proposal
from opsswarm.investigation.candidate.output import (
    CandidateEvaluation,
    CandidateModelResult,
    CandidateProposal,
)
from opsswarm.investigation.candidate.queries import validated_call
from opsswarm.investigation.candidate.tables import CandidateAttemptRow, CandidateJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.investigation.review.contracts import HypothesisReviewProposal
from opsswarm.investigation.synthesis.contracts import OpenHypothesis, OpenHypothesisRecord
from opsswarm.planning.models import ActionType, RiskLevel
from tests.unit.investigation.test_human_preparation import NOW, source_fixture


def candidate_source():
    previous = source_fixture()
    hypothesis = OpenHypothesis(
        statement="连接池异常也可能由未枚举的新原因造成",
        confidence=0.8,
        supporting_evidence_ids=("change-1",),
        unresolved_questions=("实际机制仍未证明",),
    )
    record = OpenHypothesisRecord(
        id=previous.review.open_hypothesis_id,
        investigation_run_id=previous.handoff.investigation_run_id,
        round_id=previous.review.round_id,
        investigation_attempt_id=previous.handoff.investigation_attempt_id,
        source_call_id=uuid4(),
        version=hypothesis.version,
        status=hypothesis.status,
        hypothesis=hypothesis,
        content_sha256=content_hash(hypothesis),
        created_at=NOW,
    )
    return CandidateSource(
        source_kind="terminal_handoff",
        source_id=previous.handoff.id,
        source_sha256=content_hash(previous.handoff),
        service_name="checkout-api",
        handoff=previous.handoff,
        original_route=previous.route,
        human_route=None,
        human_inputs=previous.wait.events,
        hypothesis=record,
        review=HypothesisReviewProposal(
            support_is_sufficient=True,
            rationale="证据可以支持候选评估，因果机制仍未证明",
            accepted_supporting_evidence_ids=("change-1",),
            suggested_direction="evaluate_for_routing",
        ),
        prior_input=previous.prior_input,
        evidence=(observation(),),
        call_budget=previous.call_budget,
        model_budget=previous.model_budget,
        started_at=previous.started_at,
    )


def observation(*, kind=EvidenceKind.CHANGE, evidence_id="change-1", attributes=None, age=0):
    return EvidenceObservation(
        evidence_id=evidence_id,
        kind=kind,
        source="unit-tool",
        summary="原只读结构化工具证据",
        observed_at=NOW + timedelta(seconds=age),
        attributes=attributes
        if attributes is not None
        else {
            "change_type": "deployment",
            "completed": True,
            "from_version": "v1",
            "to_version": "v2",
        },
    )


def candidate(**changes):
    return CandidateModelResult(
        proposal=CandidateProposal(
            **{
                "status": "candidate",
                "action_type": ActionType.ROLLBACK_RELEASE,
                "rationale": "评估可逆的回滚能力",
                "evidence_ids": ("change-1",),
                "preconditions": ("仍需核实现场条件并经过后续规划审查",),
                **changes,
            }
        )
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"action_type": None},
        {"evidence_ids": ()},
        {"preconditions": ()},
        {"parameters": {"target_version": "bad"}},
        {"execution_eligible": True},
        {"risk_level": "low"},
        {"action_type": "arbitrary_shell"},
        {"evidence_ids": ("change-1", "change-1")},
        {"preconditions": (" ",)},
        {"unresolved_questions": ("x" * 501,)},
    ],
)
def test_proposal_rejects_incomplete_or_authority_fields(changes):
    with pytest.raises(ValidationError):
        candidate(**changes)


def test_unknown_open_hypothesis_is_not_mapped_to_old_root_cause():
    source = candidate_source()
    result = evaluate_candidate(source, candidate())
    assert source.hypothesis.hypothesis.statement == "连接池异常也可能由未枚举的新原因造成"
    assert result.disposition == "planning_review"
    assert result.execution_eligible is False
    assert result.step.parameters.target_version == "v1"
    assert result.step.risk_level is RiskLevel.HIGH
    assert result.step.requires_human_approval is True
    assert result.step.verification.signal == "http_5xx_ratio"
    assert "root_cause" not in result.model_dump()
    with pytest.raises(ValidationError):
        CandidateEvaluation.model_validate({**result.model_dump(), "execution_eligible": True})


def test_unsupported_is_a_non_authorizing_result():
    result = evaluate_candidate(
        candidate_source(),
        candidate(
            status="unsupported",
            action_type=None,
            evidence_ids=(),
            preconditions=(),
            unresolved_questions=("没有适用的安全能力",),
        ),
    )
    assert result.disposition == "await_human"
    assert result.reason_code == "candidate_unsupported"
    assert result.step is None
    assert result.execution_eligible is False


@pytest.mark.parametrize("action_type", list(ActionType))
def test_missing_structured_parameters_remain_await_human(action_type):
    source = candidate_source().model_copy(
        update={
            "evidence": (observation(attributes={}),),
        }
    )
    result = evaluate_candidate(source, candidate(action_type=action_type))
    assert result.reason_code == "candidate_parameters_unavailable"
    assert result.disposition == "await_human"
    assert result.step is None


@pytest.mark.parametrize(
    "evidence_ids,code",
    [
        (("invented-1",), "candidate_evidence_invalid"),
        (("metric-1",), "candidate_support_missing"),
    ],
)
def test_evidence_must_be_closed_and_accepted_by_current_review(evidence_ids, code):
    source = candidate_source()
    source = source.model_copy(
        update={
            "evidence": (
                *source.evidence,
                observation(
                    evidence_id="metric-1",
                    kind=EvidenceKind.METRIC,
                    attributes={},
                ),
            )
        }
    )
    with pytest.raises(CandidateError) as caught:
        validate_proposal(source, candidate(evidence_ids=evidence_ids))
    assert caught.value.code == code


def test_human_uuid_cannot_masquerade_as_tool_evidence():
    source = candidate_source()
    human_id = str(source.human_inputs[0].id)
    source = source.model_copy(
        update={
            "evidence": (
                *source.evidence,
                observation(
                    evidence_id=human_id,
                ),
            )
        }
    )
    with pytest.raises(CandidateError, match="工具证据闭包"):
        validate_proposal(source, candidate(evidence_ids=("change-1", human_id)))


@pytest.mark.parametrize("bad_value", [True, "10", 0, 1001, 10.0])
def test_configuration_parameters_reject_bool_string_and_out_of_bounds(bad_value):
    source = candidate_source().model_copy(
        update={
            "evidence": (
                observation(
                    attributes={
                        "change_type": "configuration",
                        "completed": True,
                        "key": "database_pool_size",
                        "from_value": bad_value,
                        "to_value": 20,
                    }
                ),
            )
        }
    )
    result = evaluate_candidate(source, candidate(action_type=ActionType.ROLLBACK_CONFIGURATION))
    assert result.reason_code == "candidate_parameters_unavailable"


@pytest.mark.parametrize("healthy", [0, "false", None, True])
def test_redis_restore_requires_literal_false_metric(healthy):
    source = candidate_source().model_copy(
        update={
            "evidence": (
                observation(
                    kind=EvidenceKind.METRIC,
                    attributes={"redis_healthy": healthy},
                ),
            )
        }
    )
    assert (
        evaluate_candidate(
            source,
            candidate(
                action_type=ActionType.RESTORE_DEPENDENCY,
            ),
        ).step
        is None
    )


@pytest.mark.parametrize(
    "action_type,kind,attributes,signal",
    [
        (
            ActionType.ROLLBACK_CONFIGURATION,
            EvidenceKind.CHANGE,
            {
                "change_type": "configuration",
                "key": "database_pool_size",
                "completed": True,
                "from_value": 40,
                "to_value": 10,
            },
            "database_pool_saturation",
        ),
        (
            ActionType.RESTORE_DEPENDENCY,
            EvidenceKind.METRIC,
            {
                "redis_healthy": False,
            },
            "redis_unavailable_ratio",
        ),
    ],
)
def test_each_candidate_uses_existing_parameter_risk_and_verification_rules(
    action_type,
    kind,
    attributes,
    signal,
):
    source = candidate_source().model_copy(
        update={
            "evidence": (
                observation(attributes={}),
                observation(evidence_id="parameter-1", kind=kind, attributes=attributes),
            )
        }
    )
    result = evaluate_candidate(
        source, candidate(action_type=action_type, evidence_ids=("change-1", "parameter-1"))
    )
    assert result.disposition == "planning_review"
    assert result.step.verification.signal == signal
    assert result.step.risk_level is RiskLevel.HIGH
    assert result.step.requires_human_approval is True
    assert result.execution_eligible is False


@pytest.mark.parametrize(
    "action_type,kind,attributes",
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
        ),
        (ActionType.RESTORE_DEPENDENCY, EvidenceKind.METRIC, {"redis_healthy": False}),
    ],
)
def test_unreferenced_parameter_evidence_cannot_supply_candidate_parameters(
    action_type,
    kind,
    attributes,
):
    source = candidate_source().model_copy(
        update={
            "evidence": (
                observation(attributes={}),
                observation(evidence_id="parameter-1", kind=kind, attributes=attributes),
            ),
        }
    )
    blocked = evaluate_candidate(source, candidate(action_type=action_type))
    assert blocked.disposition == "await_human"
    assert blocked.reason_code == "candidate_parameters_unavailable"
    assert blocked.step is None
    assert blocked.execution_eligible is False
    accepted = evaluate_candidate(
        source,
        candidate(
            action_type=action_type,
            evidence_ids=("change-1", "parameter-1"),
        ),
    )
    assert accepted.disposition == "planning_review"
    assert accepted.execution_eligible is False


def test_context_preserves_complete_original_review_human_prefix_and_schema():
    source = candidate_source()
    packet = build_packet(source)
    body = json.loads(packet.model_input.messages[1].content)
    assert body["original_investigation"] == json.loads(source.prior_input.messages[1].content)
    assert body["current_review"] == source.review.model_dump(mode="json")
    assert body["human_inputs"] == [e.model_dump(mode="json") for e in source.human_inputs]
    assert len(body["capability_catalog"]) == 3
    assert packet.model_input.output_schema == CandidateProposal.model_json_schema()
    assert packet.model_input.tool_schema == convert_to_openai_tool(CandidateProposal)
    assert packet.model_input.structured_output_method == "function_calling"
    assert packet.manifest.source_sha256 == content_hash(source)
    assert packet.manifest.input_sha256 == packet.allocation.input_sha256
    assert packet.manifest.tool_names == packet.manifest.trimming == ()
    assert packet.allocation.prompt_version == "candidate-assessment-v1"


def test_context_overflow_is_rejected_without_trimming():
    source = candidate_source()
    source = source.model_copy(
        update={
            "model_budget": source.model_budget.model_copy(
                update={
                    "max_input_tokens_per_call": 100,
                }
            )
        }
    )
    with pytest.raises(CallBudgetExceeded) as caught:
        build_packet(source)
    assert caught.value.code == "model_input_budget_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "usage_kind,error_code",
    [
        ("malformed", "candidate_integrity_violation"),
        ("input_over_allocation", "candidate_call_invalid"),
        ("output_over_allocation", "candidate_call_invalid"),
        ("unknown", None),
        ("known", None),
    ],
)
async def test_historical_call_rechecks_usage_and_preserves_unknown(usage_kind, error_code):
    source = candidate_source()
    packet = build_packet(source)
    job = CandidateJobRow(id=uuid4(), investigation_run_id=source.handoff.investigation_run_id)
    owner = CandidateAttemptRow(
        id=uuid4(),
        job_id=job.id,
        investigation_run_id=job.investigation_run_id,
        started_at=NOW,
        lease_expires_at=NOW + timedelta(seconds=600),
    )
    inputs = (
        packet.allocation.estimated_input_tokens + 1
        if usage_kind == "input_over_allocation"
        else 80
    )
    outputs = (
        packet.allocation.output_token_limit + 1 if usage_kind == "output_over_allocation" else 20
    )
    usage = (
        None
        if usage_kind == "unknown"
        else {
            "input_tokens": inputs,
            "output_tokens": outputs,
            "total_tokens": 999 if usage_kind == "malformed" else inputs + outputs,
        }
    )
    call = CallReservationRow(
        id=uuid4(),
        candidate_attempt_id=owner.id,
        investigation_run_id=job.investigation_run_id,
        investigation_attempt_id=source.handoff.investigation_attempt_id,
        operation="evaluate_candidate",
        kind="model",
        status="succeeded",
        task_id=None,
        task_attempt_id=None,
        human_review_attempt_id=None,
        invocation_index=0,
        idempotency_key=f"candidate:{owner.id}",
        model_input=packet.model_input.model_dump(mode="json"),
        model_allocation=packet.allocation.model_dump(mode="json"),
        context_manifest=packet.manifest.model_dump(mode="json"),
        reserved_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
        call_result=candidate().model_dump(mode="json"),
        model_usage=usage,
    )
    session = AsyncMock()
    session.get.side_effect = [call, owner]
    if error_code:
        with pytest.raises(CandidateError) as caught:
            await validated_call(session, job, source, packet, call.id)
        assert caught.value.code == error_code
    else:
        assert await validated_call(session, job, source, packet, call.id) is call
        assert call.model_usage == usage
        if usage_kind == "unknown":
            assert call.model_usage is None
