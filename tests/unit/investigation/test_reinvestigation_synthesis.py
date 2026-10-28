"""综合输入闭包、引用边界、原预算与真实SDK离线适配。"""

import json
from datetime import timedelta
from uuid import uuid4

import httpx2
import pytest

from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.reinvestigation_execution.context import build_packet as task_packet
from opsswarm.investigation.reinvestigation_execution.contracts import ExecutionResult
from opsswarm.investigation.reinvestigation_synthesis.context import build_packet
from opsswarm.investigation.reinvestigation_synthesis.contracts import (
    SynthesisError,
    SynthesisProfessional,
    SynthesisSource,
)
from opsswarm.investigation.reinvestigation_synthesis.model import SynthesisModel
from opsswarm.investigation.reinvestigation_synthesis.policy import (
    check_budget,
    validate_proposal,
)
from opsswarm.investigation.synthesis.contracts import OpenHypothesisProposal
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.unit.investigation.test_reflow_context_policy import budget_snapshot
from tests.unit.investigation.test_reinvestigation_execution_materials import (
    material_fixture,
    report_for,
)


def source_fixture():
    source, dispatch, assignment, identity, window, snapshot = material_fixture()
    packet = task_packet(source, dispatch, assignment, identity, window, snapshot)
    result = ExecutionResult(
        investigation_run_id=source.investigation_run_id,
        task_id=identity,
        dispatch_sha256=content_hash(dispatch),
        evidence_call_id=uuid4(),
        model_call_id=uuid4(),
        evidence_sha256=packet.manifest.evidence_sha256,
        packet_sha256=content_hash(packet),
        report=report_for(snapshot),
        created_at=dispatch.created_at + timedelta(seconds=1),
    )
    return SynthesisSource.model_validate(
        source.model_dump(mode="json")
        | {
            "version": "reinvestigation-synthesis-source-v1",
            "created_at": result.created_at + timedelta(seconds=1),
            "coordination_created_at": source.created_at,
            "coordination_source_sha256": content_hash(source),
            "dispatch": dispatch,
            "dispatch_sha256": content_hash(dispatch),
            "professionals": (
                SynthesisProfessional(
                    task_id=identity,
                    assignment=assignment,
                    effective_window=window,
                    evidence=snapshot,
                    result=result,
                ),
            ),
        }
    )


def proposal(source, **changes):
    return OpenHypothesisProposal(
        **(
            {
                "statement": "新证据提示异常但需独立审查",
                "confidence": 0.2,
                "supporting_evidence_ids": (
                    source.professionals[0].evidence.evidence[0].evidence_id,
                ),
                "unresolved_questions": ("其他依赖是否异常？",),
            }
            | changes
        )
    )


def test_full_materials_professionals_and_unobserved_windows_enter_frozen_input():
    source = source_fixture()
    packet = build_packet(source)
    body = json.loads(packet.model_input.messages[1].content)
    assert body == source.model_dump(mode="json")
    assert len(body["materials"]) == 10
    assert body["professionals"][0]["effective_window"]["adjustments"]
    assert packet.manifest.professional_hashes == (content_hash(source.professionals[0]),)
    assert packet.manifest.trimming == packet.manifest.tool_names == ()
    validate_proposal(source, proposal(source))


@pytest.mark.parametrize("field", ["dispatch_sha256", "coordination_source_sha256"])
def test_forged_root_hashes_rejected(field):
    with pytest.raises(SynthesisError):
        build_packet(source_fixture().model_copy(update={field: "0" * 64}))


@pytest.mark.parametrize(
    "field,value",
    [
        ("task_id", uuid4()),
        ("packet_sha256", "0" * 64),
        ("evidence_sha256", "0" * 64),
        ("dispatch_sha256", "0" * 64),
    ],
)
def test_forged_professional_result_rejected(field, value):
    source = source_fixture()
    p = source.professionals[0]
    p = p.model_copy(update={"result": p.result.model_copy(update={field: value})})
    with pytest.raises(SynthesisError):
        build_packet(source.model_copy(update={"professionals": (p,)}))


@pytest.mark.parametrize("field", ["supporting_evidence_ids", "counter_evidence_ids"])
def test_old_or_foreign_evidence_cannot_support_current_hypothesis(field):
    source = source_fixture()
    with pytest.raises(SynthesisError):
        validate_proposal(source, proposal(source, **{field: ("historical-evidence",)}))


def test_missing_professionals_cannot_be_silently_dropped():
    with pytest.raises(SynthesisError):
        build_packet(source_fixture().model_copy(update={"professionals": ()}))


def test_necessary_context_over_budget_stops_before_model():
    source = source_fixture()
    source = source.model_copy(
        update={
            "model_budget": source.model_budget.model_copy(update={"max_input_tokens_per_call": 1})
        }
    )
    # 修改原冻结策略也应先被原来源摘要核验拒绝，不能绕过总账本。
    with pytest.raises((SynthesisError, CallBudgetExceeded)):
        build_packet(source)


@pytest.mark.parametrize(
    "change", [{"max_model_calls": 0}, {"max_tool_calls": 0}, {"max_tool_calls_per_task": 0}]
)
def test_original_policy_mismatch_is_not_a_new_budget(change):
    source = source_fixture()
    packet = build_packet(source)
    budget = budget_snapshot(source)
    budget = budget.model_copy(update={"policy": source.call_budget.model_copy(update=change)})
    with pytest.raises(SynthesisError):
        check_budget(source, packet, budget, now=source.created_at, allocate=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("usage", [True, False])
async def test_real_sdk_preserves_input_schema_and_unknown_usage(usage):
    source = source_fixture()
    packet = build_packet(source)
    seen = []

    def respond(request):
        body = json.loads(request.content)
        seen.append(body)
        return sdk_response(body, proposal(source).model_dump(mode="json"), usage=usage)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(respond)) as client:
        model = SynthesisModel(sdk_model(client), source.model_budget)
        result = await model.invoke(packet.model_input)
    assert result.proposal == proposal(source)
    assert (model.usage is not None) == usage
    assert len(seen) == 1
    assert seen[0]["messages"][1]["content"] == packet.model_input.messages[1].content
    assert seen[0]["tools"][0] == packet.model_input.tool_schema


def test_empty_new_evidence_stops_synthesis_instead_of_borrowing_history():
    from opsswarm.investigation.reinvestigation_synthesis.policy import prepare_blocker

    source = source_fixture()
    professional = source.professionals[0]
    empty = professional.evidence.model_copy(update={"evidence": ()})
    source = source.model_copy(
        update={"professionals": (professional.model_copy(update={"evidence": empty}),)}
    )
    assert prepare_blocker(source) == "synthesis_evidence_missing"


def test_original_deadline_does_not_restart_when_professionals_finish():
    source = source_fixture()
    packet = build_packet(source)
    deadline = source.started_at + timedelta(seconds=source.call_budget.max_duration_seconds)
    with pytest.raises(CallBudgetExceeded) as error:
        check_budget(source, packet, budget_snapshot(source), now=deadline, allocate=True)
    assert error.value.code == "run_deadline_exceeded"


def test_only_own_synthesis_reservation_can_be_in_flight():
    from opsswarm.investigation.budget.contracts import CallOperation
    from tests.unit.investigation.test_reflow_context_policy import model_call

    source = source_fixture()
    packet = build_packet(source)
    call = model_call(
        source,
        packet,
        status="reserved",
        operation=CallOperation.SYNTHESIZE_REINVESTIGATION,
        reinvestigation_attempt_id=None,
        reflow_synthesis_attempt_id=uuid4(),
    )
    budget = budget_snapshot(source, (call,))
    check_budget(source, packet, budget, now=source.created_at, allocate=False, own_call_id=call.id)
    with pytest.raises(CallBudgetExceeded) as error:
        check_budget(source, packet, budget, now=source.created_at, allocate=False)
    assert error.value.code == "calls_in_flight"


def test_unknown_attempt_still_occupies_original_call_capacity():
    from tests.unit.investigation.test_reflow_context_policy import model_call

    source = source_fixture()
    packet = build_packet(source)
    source = source.model_copy(
        update={"call_budget": source.call_budget.model_copy(update={"max_model_calls": 1})}
    )
    budget = budget_snapshot(source, (model_call(source, packet),))
    with pytest.raises(CallBudgetExceeded) as error:
        check_budget(source, packet, budget, now=source.created_at, allocate=True)
    assert error.value.code == "model_call_budget_exceeded"
    assert budget.model_usage.unknown_model_calls == 1


def test_real_usage_over_allocation_cannot_publish_hypothesis():
    from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
    from tests.unit.investigation.test_reflow_context_policy import model_call

    source = source_fixture()
    packet = build_packet(source)
    inputs = packet.allocation.estimated_input_tokens + 1
    call = model_call(
        source,
        packet,
        model_usage=ReportedModelUsage(input_tokens=inputs, output_tokens=0, total_tokens=inputs),
    )
    with pytest.raises(CallBudgetExceeded) as error:
        check_budget(
            source, packet, budget_snapshot(source, (call,)), now=source.created_at, allocate=False
        )
    assert error.value.code == "model_usage_exceeded_allocation"
