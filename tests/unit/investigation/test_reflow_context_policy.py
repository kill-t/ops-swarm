"""纯契约核验必要上下文闭包、原容量与共享账本的结算边界。"""

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.investigation.budget.contracts import (
    CallBudgetPolicy,
    CallBudgetSnapshot,
    CallOperation,
    CallReservationRecord,
)
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.budget.model_policy import allocate_input, summarize_model_usage
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.coordination.contracts import CoordinationPolicy, CoordinationProposal
from opsswarm.investigation.reinvestigation_coordination.context import build_packet
from opsswarm.investigation.reinvestigation_coordination.contracts import (
    PROMPT_VERSION,
    ReflowError,
    ReflowMaterial,
    ReflowSource,
)
from opsswarm.investigation.reinvestigation_coordination.policy import (
    check_budget,
    prepare_blocker,
    validate_proposal,
)
from tests.model_budget_helpers import model_policy

NOW = datetime(2026, 10, 27, 3, tzinfo=UTC)
KINDS = (
    "original_investigation",
    "current_review",
    "human_inputs",
    "candidate",
    "action_plan",
    "conditions",
    "execution",
    "recovery",
    "convergence",
    "admission",
)


def reflow_source(**changes):
    policy = model_policy()
    policy = policy.model_copy(
        update={
            "profile": policy.profile.model_copy(
                update={"structured_output_method": "function_calling"}
            )
        }
    )
    materials = []
    for index, kind in enumerate(KINDS):
        content = {"kind": kind, "constraint": "禁止重启，保留反证与未解问题", "index": index}
        materials.append(
            ReflowMaterial(
                kind=kind,
                reference_id=f"frozen:{kind}",
                content_sha256=raw_content_hash(content),
                content=content,
            )
        )
    return ReflowSource(
        **(
            {
                "investigation_run_id": uuid4(),
                "incident_id": uuid4(),
                "admission_sha256": "a" * 64,
                "incident_version": 5,
                "parent_attempt_id": uuid4(),
                "coordination_policy": CoordinationPolicy(),
                "next_round_number": 2,
                "existing_task_count": 3,
                "maximum_tasks": 3,
                "call_budget": CallBudgetPolicy(),
                "model_budget": policy,
                "started_at": NOW - timedelta(seconds=30),
                "created_at": NOW,
                "materials": tuple(materials),
            }
            | changes
        )
    )


def model_call(source, packet, *, sequence=1, status="unknown", **changes):
    owner = uuid4()
    return CallReservationRecord(
        **(
            {
                "id": uuid4(),
                "investigation_run_id": source.investigation_run_id,
                "investigation_attempt_id": source.parent_attempt_id,
                "reinvestigation_attempt_id": owner,
                "task_id": None,
                "task_attempt_id": None,
                "sequence": sequence,
                "invocation_index": 0,
                "operation": CallOperation.COORDINATE_REINVESTIGATION,
                "kind": "model",
                "status": status,
                "error_code": None,
                "reserved_at": NOW,
                "finished_at": None if status == "reserved" else NOW,
                "model_allocation": packet.allocation,
                "model_usage": None,
            }
            | changes
        )
    )


def budget_snapshot(source, calls=()):
    return CallBudgetSnapshot(
        investigation_run_id=source.investigation_run_id,
        policy=source.call_budget,
        model_budget=source.model_budget,
        started_at=source.started_at,
        deadline=source.started_at + timedelta(seconds=source.call_budget.max_duration_seconds),
        reserved_model_calls=sum(c.kind == "model" for c in calls),
        reserved_tool_calls=sum(c.kind == "tool" for c in calls),
        calls=calls,
        model_usage=summarize_model_usage(
            source.model_budget,
            [(c.model_allocation, c.model_usage) for c in calls if c.model_allocation is not None],
        ),
    )


def assert_budget_error(code, source, packet, budget, **kwargs):
    with pytest.raises(CallBudgetExceeded) as caught:
        check_budget(source, packet, budget, now=NOW, **kwargs)
    assert caught.value.code == code


def proposal(round_number=2, count=1):
    return CoordinationProposal(
        round_number=round_number,
        goal="调查恢复失败并保留原人工约束",
        tasks=tuple(
            {
                "task_key": f"followup_{role}",
                "role": role,
                "goal": f"补查{role}缺口",
                "lookback_seconds": 300,
            }
            for role in ("log", "metric", "change")[:count]
        ),
    )


def test_packet_contains_every_full_material_and_exact_manifest_reference():
    source = reflow_source()
    packet = build_packet(source)
    payload = json.loads(packet.model_input.messages[1].content)
    assert payload["materials"] == [m.model_dump(mode="json") for m in source.materials]
    assert payload["next_round_number"] == 2
    assert payload["existing_task_count"] == 3
    assert payload["maximum_tasks"] == 3
    assert packet.manifest.materials == tuple(
        {
            "kind": m.kind,
            "reference_id": m.reference_id,
            "content_sha256": m.content_sha256,
        }
        for m in source.materials
    )
    assert packet.manifest.source_sha256 == content_hash(source)
    assert packet.manifest.input_sha256 == packet.allocation.input_sha256
    assert packet.manifest.tool_names == packet.manifest.trimming == ()
    assert packet.model_input.structured_output_method == "function_calling"
    assert packet.model_input.output_schema == CoordinationProposal.model_json_schema()
    assert packet.model_input.tool_schema == convert_to_openai_tool(CoordinationProposal)
    assert packet.allocation == allocate_input(
        source.model_budget, packet.model_input, prompt_version=PROMPT_VERSION
    )


@pytest.mark.parametrize(
    "change,code",
    [
        ({"next_round_number": 4}, "reflow_round_limit_exceeded"),
        ({"existing_task_count": 6, "maximum_tasks": 0}, "reflow_task_limit_exceeded"),
    ],
)
def test_exhausted_original_capacity_never_produces_usable_packet(change, code):
    source = reflow_source(**change)
    assert prepare_blocker(source) == code
    with pytest.raises(ReflowError) as caught:
        build_packet(source)
    assert caught.value.code == code


@pytest.mark.parametrize("corruption", ["hash", "duplicate_kind", "unknown_kind"])
def test_necessary_materials_reject_hash_or_kind_corruption(corruption):
    source = reflow_source()
    first = source.materials[0]
    change = {
        "hash": {"content_sha256": "0" * 64},
        "duplicate_kind": {"kind": source.materials[1].kind},
        "unknown_kind": {"kind": "arbitrary_missing_original"},
    }[corruption]
    source = source.model_copy(
        update={
            "materials": (
                first.model_copy(update=change),
                *source.materials[1:],
            )
        }
    )
    with pytest.raises(ReflowError) as caught:
        build_packet(source)
    assert caught.value.code == "reflow_integrity_violation"


@pytest.mark.parametrize("count", [9, 11])
def test_source_requires_exactly_ten_materials(count):
    source = reflow_source()
    material = source.materials[0]
    with pytest.raises(ValidationError):
        ReflowSource.model_validate(source.model_dump() | {"materials": (material,) * count})


@pytest.mark.parametrize("failure", ["input_budget", "byte_limit"])
def test_required_materials_over_budget_stop_without_trimming(failure):
    source = reflow_source()
    if failure == "input_budget":
        source = source.model_copy(
            update={
                "model_budget": source.model_budget.model_copy(
                    update={"max_input_tokens_per_call": 1000}
                )
            }
        )
    else:
        content = {"required_human_constraint": "x" * 262145}
        source = source.model_copy(
            update={
                "materials": (
                    source.materials[0].model_copy(
                        update={
                            "content": content,
                            "content_sha256": raw_content_hash(content),
                        }
                    ),
                    *source.materials[1:],
                )
            }
        )
    with pytest.raises(CallBudgetExceeded) as caught:
        build_packet(source)
    assert caught.value.code == "model_input_budget_exceeded"


def test_proposal_requires_exact_next_round_and_remaining_task_capacity():
    source = reflow_source(maximum_tasks=1, existing_task_count=5)
    validate_proposal(source, proposal())
    for result in (proposal(round_number=1), proposal(round_number=3), proposal(count=2)):
        with pytest.raises(ReflowError) as caught:
            validate_proposal(source, result)
        assert caught.value.code == "reflow_proposal_invalid"


@pytest.mark.parametrize("duplicate", ["task_key", "role"])
def test_proposal_rejects_duplicate_task_keys_or_roles(duplicate):
    data = proposal(count=2).model_dump()
    data["tasks"][1][duplicate] = data["tasks"][0][duplicate]
    with pytest.raises(ValidationError):
        CoordinationProposal.model_validate(data)


def test_allocation_uses_actual_frozen_input_instead_of_full_per_call_ceiling():
    source = reflow_source()
    packet = build_packet(source)
    assert packet.allocation.estimated_input_tokens < source.model_budget.max_input_tokens_per_call
    source = source.model_copy(
        update={
            "model_budget": source.model_budget.model_copy(
                update={
                    "max_input_tokens": packet.allocation.estimated_input_tokens,
                    "max_output_tokens": packet.allocation.output_token_limit,
                    "max_estimated_cost_usd": packet.allocation.reserved_cost_usd,
                }
            )
        }
    )
    packet = build_packet(source)
    check_budget(source, packet, budget_snapshot(source), now=NOW, allocate=True)


def test_exact_own_reservation_can_settle_at_final_model_call_without_new_capacity():
    source = reflow_source(call_budget=CallBudgetPolicy(max_model_calls=1))
    packet = build_packet(source)
    call = model_call(source, packet, status="reserved")
    budget = budget_snapshot(source, (call,))
    check_budget(source, packet, budget, now=NOW, allocate=False, own_call_id=call.id)
    assert_budget_error(
        "model_call_budget_exceeded", source, packet, budget, allocate=True, own_call_id=call.id
    )
    settled = call.model_copy(update={"status": "succeeded", "finished_at": NOW})
    check_budget(source, packet, budget_snapshot(source, (settled,)), now=NOW, allocate=False)


@pytest.mark.parametrize("forgery", ["wrong_id", "operation", "owner", "parent", "allocation"])
def test_only_precise_own_call_is_exempt_from_inflight_blocker(forgery):
    source = reflow_source()
    packet = build_packet(source)
    call = model_call(source, packet, status="reserved")
    changes = {
        "wrong_id": {},
        "operation": {"operation": CallOperation.EVALUATE_CANDIDATE},
        "owner": {"reinvestigation_attempt_id": None},
        "parent": {"investigation_attempt_id": uuid4()},
        "allocation": {
            "model_allocation": packet.allocation.model_copy(
                update={"estimated_input_tokens": packet.allocation.estimated_input_tokens + 1}
            )
        },
    }[forgery]
    changed = call.model_copy(update=changes)
    assert_budget_error(
        "calls_in_flight",
        source,
        packet,
        budget_snapshot(source, (changed,)),
        allocate=False,
        own_call_id=uuid4() if forgery == "wrong_id" else call.id,
    )


def test_other_inflight_reservation_still_blocks_even_when_own_call_is_precise():
    source = reflow_source()
    packet = build_packet(source)
    own = model_call(source, packet, status="reserved")
    other = model_call(source, packet, sequence=2, status="reserved")
    assert_budget_error(
        "calls_in_flight",
        source,
        packet,
        budget_snapshot(source, (own, other)),
        allocate=False,
        own_call_id=own.id,
    )


def test_unknown_usage_preserves_original_allocation_and_does_not_refund_capacity():
    source = reflow_source()
    packet = build_packet(source)
    source = source.model_copy(
        update={
            "model_budget": source.model_budget.model_copy(
                update={
                    "max_input_tokens": packet.allocation.estimated_input_tokens,
                }
            )
        }
    )
    packet = build_packet(source)
    call = model_call(source, packet)
    budget = budget_snapshot(source, (call,))
    assert budget.model_usage.unknown_model_calls == 1
    assert budget.model_usage.reported_cost_usd is None
    assert budget.model_usage.committed_input_tokens == packet.allocation.estimated_input_tokens
    check_budget(source, packet, budget, now=NOW, allocate=False)
    assert_budget_error("run_input_budget_exceeded", source, packet, budget, allocate=True)


@pytest.mark.parametrize("dimension", ["input", "output"])
def test_reported_usage_over_allocation_is_audited_but_never_publishable(dimension):
    source = reflow_source()
    packet = build_packet(source)
    inputs = packet.allocation.estimated_input_tokens + int(dimension == "input")
    outputs = packet.allocation.output_token_limit + int(dimension == "output")
    call = model_call(
        source,
        packet,
        model_usage=ReportedModelUsage(
            input_tokens=inputs,
            output_tokens=outputs,
            total_tokens=inputs + outputs,
        ),
    )
    assert_budget_error(
        "model_usage_exceeded_allocation",
        source,
        packet,
        budget_snapshot(source, (call,)),
        allocate=False,
    )


@pytest.mark.parametrize(
    "tamper",
    [
        "root",
        "call_root",
        "policy",
        "model_policy",
        "start",
        "deadline",
        "missing_deadline",
        "sequence",
        "model_count",
        "tool_count",
        "summary",
        "missing_summary",
    ],
)
def test_budget_rejects_root_policy_clock_sequence_counts_or_summary_tampering(tamper):
    source = reflow_source()
    packet = build_packet(source)
    call = model_call(source, packet)
    budget = budget_snapshot(source, (call,))
    changes = {
        "root": {"investigation_run_id": uuid4()},
        "call_root": {"calls": (call.model_copy(update={"investigation_run_id": uuid4()}),)},
        "policy": {"policy": source.call_budget.model_copy(update={"max_model_calls": 11})},
        "model_policy": {
            "model_budget": source.model_budget.model_copy(update={"max_input_tokens": 1})
        },
        "start": {"started_at": source.started_at + timedelta(seconds=1)},
        "deadline": {"deadline": budget.deadline + timedelta(seconds=1)},
        "missing_deadline": {"deadline": None},
        "sequence": {"calls": (call.model_copy(update={"sequence": 2}),)},
        "model_count": {"reserved_model_calls": 0},
        "tool_count": {"reserved_tool_calls": 1},
        "summary": {
            "model_usage": budget.model_usage.model_copy(
                update={
                    "committed_cost_usd": Decimal("0"),
                }
            )
        },
        "missing_summary": {"model_usage": None},
    }[tamper]
    with pytest.raises(ReflowError) as caught:
        check_budget(source, packet, budget.model_copy(update=changes), now=NOW, allocate=False)
    assert caught.value.code == "reflow_integrity_violation"


def test_actual_run_deadline_cannot_be_extended_by_a_claimed_snapshot_deadline():
    source = reflow_source(started_at=NOW - timedelta(seconds=300))
    packet = build_packet(source)
    with pytest.raises(CallBudgetExceeded) as caught:
        check_budget(source, packet, budget_snapshot(source), now=NOW, allocate=False)
    assert caught.value.code == "run_deadline_exceeded"


@pytest.mark.parametrize("limit", ["total", "task"])
def test_old_tool_usage_still_obeys_global_and_per_task_limits(limit):
    policy = CallBudgetPolicy(
        max_tool_calls=1 if limit == "total" else 3,
        max_tool_calls_per_task=3 if limit == "total" else 1,
    )
    source = reflow_source(call_budget=policy)
    packet = build_packet(source)
    task_id = uuid4()
    first = model_call(
        source,
        packet,
        kind="tool",
        operation=CallOperation.QUERY_LOGS,
        reinvestigation_attempt_id=None,
        model_allocation=None,
        task_id=task_id,
        task_attempt_id=uuid4(),
    )
    second = model_call(
        source,
        packet,
        sequence=2,
        kind="tool",
        operation=CallOperation.QUERY_LOGS,
        reinvestigation_attempt_id=None,
        model_allocation=None,
        task_id=uuid4() if limit == "total" else task_id,
        task_attempt_id=uuid4(),
    )
    check_budget(source, packet, budget_snapshot(source, (first,)), now=NOW, allocate=False)
    assert_budget_error(
        "tool_call_budget_exceeded",
        source,
        packet,
        budget_snapshot(source, (first, second)),
        allocate=False,
    )


def test_already_committed_calls_cannot_exceed_original_model_call_limit():
    source = reflow_source(call_budget=CallBudgetPolicy(max_model_calls=1))
    packet = build_packet(source)
    calls = (model_call(source, packet), model_call(source, packet, sequence=2))
    assert_budget_error(
        "model_call_budget_exceeded",
        source,
        packet,
        budget_snapshot(source, calls),
        allocate=False,
    )
