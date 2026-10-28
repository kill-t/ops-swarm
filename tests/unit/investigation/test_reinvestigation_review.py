"""独立回流审查的精确提案、引用边界、原预算与SDK契约。"""

import json
from datetime import timedelta
from uuid import uuid4

import httpx2
import pytest

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.reinvestigation_review.context import PROMPT, build_packet
from opsswarm.investigation.reinvestigation_review.contracts import (
    ReflowReviewError,
    ReflowReviewSource,
)
from opsswarm.investigation.reinvestigation_review.model import ReflowReviewModel
from opsswarm.investigation.reinvestigation_review.policy import check_budget, validate_proposal
from opsswarm.investigation.reinvestigation_synthesis.context import PROMPT as SYNTHESIS_PROMPT
from opsswarm.investigation.reinvestigation_synthesis.context import (
    build_packet as synthesis_packet,
)
from opsswarm.investigation.reinvestigation_synthesis.contracts import SynthesisResult
from opsswarm.investigation.review.contracts import HypothesisReviewProposal
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.unit.investigation.test_reflow_context_policy import budget_snapshot, model_call
from tests.unit.investigation.test_reinvestigation_synthesis import proposal as synthesis_proposal
from tests.unit.investigation.test_reinvestigation_synthesis import (
    source_fixture as synthesis_fixture,
)


def source_fixture():
    origin = synthesis_fixture()
    packet = synthesis_packet(origin)
    result = SynthesisResult(
        investigation_run_id=origin.investigation_run_id,
        job_id=origin.investigation_run_id,
        source_call_id=uuid4(),
        source_sha256=content_hash(origin),
        proposal=synthesis_proposal(origin),
        created_at=origin.created_at + timedelta(seconds=1),
    )
    return ReflowReviewSource.model_validate(
        origin.model_dump(mode="json")
        | {
            "version": "reinvestigation-review-source-v1",
            "created_at": result.created_at + timedelta(seconds=1),
            "synthesis_created_at": origin.created_at,
            "synthesis_source_sha256": content_hash(origin),
            "synthesis_packet_sha256": content_hash(packet),
            "synthesis_result": result,
            "synthesis_result_sha256": content_hash(result),
        }
    )


def proposal(source, direction="evaluate_for_routing", **changes):
    body = {
        "support_is_sufficient": direction == "evaluate_for_routing",
        "rationale": "按精确提案和新证据独立审查，不证明根因",
        "accepted_supporting_evidence_ids": source.synthesis_result.proposal.supporting_evidence_ids
        if direction == "evaluate_for_routing"
        else (),
        "suggested_direction": direction,
        "evidence_gaps": () if direction == "evaluate_for_routing" else ("仍缺少独立因果证据",),
        "follow_up_suggestions": (
            {"role": "log", "question": "检查同窗口依赖异常", "rationale": "需要排除其他原因"},
        )
        if direction == "follow_up"
        else (),
    }
    return HypothesisReviewProposal(**(body | changes))


def test_full_exact_proposal_materials_and_windows_enter_independent_context():
    source = source_fixture()
    packet = build_packet(source)
    assert json.loads(packet.model_input.messages[1].content) == source.model_dump(mode="json")
    assert source.synthesis_result.review_status == "pending_review"
    assert len(source.materials) == 10
    assert packet.manifest.synthesis_result_sha256 == content_hash(source.synthesis_result)
    assert packet.manifest.role_version == "reinvestigation-reviewer-v1"
    assert packet.manifest.tool_names == packet.manifest.trimming == ()
    assert PROMPT != SYNTHESIS_PROMPT


@pytest.mark.parametrize(
    "field", ["synthesis_source_sha256", "synthesis_packet_sha256", "synthesis_result_sha256"]
)
def test_wrong_synthesis_identity_rejected(field):
    with pytest.raises(ReflowReviewError):
        build_packet(source_fixture().model_copy(update={field: "0" * 64}))


@pytest.mark.parametrize(
    "field,value",
    [("job_id", uuid4()), ("investigation_run_id", uuid4()), ("source_sha256", "0" * 64)],
)
def test_rehashed_foreign_proposal_still_rejected(field, value):
    source = source_fixture()
    result = source.synthesis_result.model_copy(update={field: value})
    source = source.model_copy(
        update={"synthesis_result": result, "synthesis_result_sha256": content_hash(result)}
    )
    with pytest.raises(ReflowReviewError):
        build_packet(source)


@pytest.mark.parametrize("direction", ["evaluate_for_routing", "follow_up", "human_review"])
def test_three_finite_directions_are_opinions_only(direction):
    source = source_fixture()
    p = proposal(source, direction)
    validate_proposal(source, p)
    assert not any(k in p.model_dump() for k in ["execution_eligible", "approved", "task_id"])


@pytest.mark.parametrize("field", ["accepted_supporting_evidence_ids", "counter_evidence_ids"])
def test_historical_or_foreign_evidence_cannot_be_accepted(field):
    source = source_fixture()
    with pytest.raises(ReflowReviewError):
        validate_proposal(source, proposal(source, **{field: ("historical-only",)}))


def test_unknown_reservation_keeps_original_capacity():
    source = source_fixture()
    packet = build_packet(source)
    source = source.model_copy(
        update={"call_budget": source.call_budget.model_copy(update={"max_model_calls": 1})}
    )
    call = model_call(
        source,
        packet,
        operation=CallOperation.REVIEW_REINVESTIGATION,
        reinvestigation_attempt_id=None,
        reflow_review_attempt_id=uuid4(),
    )
    with pytest.raises(CallBudgetExceeded) as error:
        check_budget(
            source, packet, budget_snapshot(source, (call,)), now=source.created_at, allocate=True
        )
    assert error.value.code == "model_call_budget_exceeded"


def test_only_current_review_owner_can_have_inflight_call():
    source = source_fixture()
    packet = build_packet(source)
    call = model_call(
        source,
        packet,
        status="reserved",
        operation=CallOperation.REVIEW_REINVESTIGATION,
        reinvestigation_attempt_id=None,
        reflow_review_attempt_id=uuid4(),
    )
    budget = budget_snapshot(source, (call,))
    check_budget(source, packet, budget, now=source.created_at, allocate=False, own_call_id=call.id)
    foreign = call.model_copy(
        update={
            "operation": CallOperation.SYNTHESIZE_REINVESTIGATION,
            "reflow_review_attempt_id": None,
            "reflow_synthesis_attempt_id": uuid4(),
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


def test_original_deadline_does_not_restart_for_review():
    source = source_fixture()
    packet = build_packet(source)
    with pytest.raises(CallBudgetExceeded) as error:
        check_budget(
            source,
            packet,
            budget_snapshot(source),
            now=source.started_at + timedelta(seconds=source.call_budget.max_duration_seconds),
            allocate=True,
        )
    assert error.value.code == "run_deadline_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("direction", ["evaluate_for_routing", "follow_up", "human_review"])
@pytest.mark.parametrize("usage", [True, False])
async def test_real_sdk_uses_independent_review_schema_and_preserves_usage(direction, usage):
    source = source_fixture()
    packet = build_packet(source)
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        return sdk_response(body, proposal(source, direction).model_dump(mode="json"), usage=usage)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        model = ReflowReviewModel(sdk_model(client), source.model_budget)
        result = await model.invoke(packet.model_input)
    assert result.proposal == proposal(source, direction)
    assert (model.usage is not None) == usage
    assert seen[0]["messages"][0]["content"] == PROMPT
    assert seen[0]["tools"][0] == packet.model_input.tool_schema
