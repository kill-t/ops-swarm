"""回流业务分派不扩容、不借用旧证据，并独立核对编译来源。"""

from datetime import timedelta
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.investigation.budget.contracts import CallBudgetPolicy, CallOperation
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.budget.model_policy import estimate_cost
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.coordination.contracts import CoordinationPolicy
from opsswarm.investigation.reinvestigation_coordination.context import build_packet
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowResult
from opsswarm.investigation.reinvestigation_dispatch.policy import (
    capacity_blocker,
    compile_dispatch,
    stable_id,
    task_id,
)
from tests.unit.investigation.test_reflow_context_policy import (
    NOW,
    budget_snapshot,
    model_call,
    proposal,
    reflow_source,
)

DISPATCH_AT = NOW + timedelta(seconds=10)
FLOOR = NOW - timedelta(seconds=10)


def dispatch_source(*, floor=FLOOR, **changes):
    source = reflow_source(**changes)
    materials = []
    for material in source.materials:
        if material.kind == "recovery":
            body = {"result": {"created_at": floor.isoformat()}}
            material = material.model_copy(
                update={"content": body, "content_sha256": raw_content_hash(body)}
            )
        materials.append(material)
    return source.model_copy(update={"materials": tuple(materials)})


def coordination_result(source, *, count=1, **changes):
    return ReflowResult(
        **(
            {
                "investigation_run_id": source.investigation_run_id,
                "job_id": source.investigation_run_id,
                "source_call_id": uuid4(),
                "source_sha256": content_hash(source),
                "proposal": proposal(count=count),
                "created_at": NOW + timedelta(seconds=1),
            }
            | changes
        )
    )


def compile_result(source, result=None, *, budget=None, now=DISPATCH_AT, **changes):
    result = result or coordination_result(source)
    return compile_dispatch(
        source,
        result,
        **(
            {
                "result_sha256": content_hash(result),
                "service_name": "order-service",
                "budget": budget if budget is not None else budget_snapshot(source),
                "now": now,
                "basis_event_cursor": 1,
            }
            | changes
        ),
    )


def test_stable_round_and_task_identity_includes_root_key_and_version():
    root = UUID("12345678-1234-1234-1234-123456789abc")
    assert stable_id(root, "reflow-round-v1") == stable_id(root, "reflow-round-v1")
    assert task_id(root, "followup_log") == task_id(root, "followup_log")
    assert (
        len(
            {
                stable_id(root, "reflow-round-v1"),
                stable_id(root, "reflow-round-v2"),
                task_id(root, "followup_log"),
                task_id(root, "followup_metric"),
                task_id(uuid4(), "followup_log"),
            }
        )
        == 5
    )


def test_custom_task_keys_and_system_injected_scope_keep_original_proposal():
    source = dispatch_source()
    result = coordination_result(source, count=3)
    record = compile_result(source, result)
    assert record.status == "queued"
    assert record.reason_code is None
    assert record.round_id == stable_id(source.investigation_run_id, "reflow-round-v1")
    assert record.assignment is not None
    assert record.assignment.round_number == source.next_round_number
    assert {t.task_key for t in record.assignment.tasks} == {
        "followup_log",
        "followup_metric",
        "followup_change",
    }
    assert all(t.service_name == "order-service" for t in record.assignment.tasks)
    assert all(t.evidence_snapshot_id is None for t in record.assignment.tasks)
    assert all(
        t.max_attempts == source.coordination_policy.max_attempts_per_task
        for t in record.assignment.tasks
    )
    assert record.evidence_policy_version == "post-verification-read-only-v1"
    assert record.execution_policy_version == "reinvestigation-task-v1"
    assert record.budget == budget_snapshot(source)
    assert result.dispatch_status == "not_dispatched"


@pytest.mark.parametrize("role,seconds", [("log", 900), ("metric", 900), ("change", 21600)])
def test_authorized_role_maximum_window_is_allowed_and_one_second_more_is_blocked(role, seconds):
    source = dispatch_source()
    original = next(t for t in proposal(count=3).tasks if t.role.value == role)
    for lookback, status in ((seconds, "queued"), (seconds + 1, "blocked")):
        # change的结构契约已经限制21600；越界测试通过未受信任复制模拟领域入口。
        task = original.model_copy(update={"lookback_seconds": lookback})
        result = coordination_result(
            source, proposal=proposal().model_copy(update={"tasks": (task,)})
        )
        record = compile_result(source, result)
        assert record.status == status
        if status == "blocked":
            assert record.reason_code == "reflow_task_window_exceeded"
            assert record.round_id is record.assignment is None
        else:
            assert record.assignment.tasks[0].window_start == max(
                FLOOR, DISPATCH_AT - timedelta(seconds=seconds)
            )


def test_new_evidence_window_is_clipped_at_failed_verification_without_old_snapshot():
    source = dispatch_source()
    record = compile_result(source)
    task = record.assignment.tasks[0]
    assert task.window_start == FLOOR
    assert task.window_end == DISPATCH_AT
    assert task.evidence_snapshot_id is None
    assert record.observation_floor_at == FLOOR


def test_zero_post_verification_window_never_creates_tasks():
    source = dispatch_source(floor=DISPATCH_AT)
    result = coordination_result(source, created_at=DISPATCH_AT)
    record = compile_result(source, result)
    assert record.status == "blocked"
    assert record.reason_code == "reflow_window_not_ready"
    assert record.round_id is record.assignment is None


@pytest.mark.parametrize("floor", [DISPATCH_AT, DISPATCH_AT.replace(tzinfo=None)])
def test_verification_floor_cannot_be_future_or_without_timezone(floor):
    source = dispatch_source(floor=floor)
    with pytest.raises(ValueError):
        compile_result(source)


@pytest.mark.parametrize(
    "change",
    [
        {"next_round_number": 3},
        {"maximum_tasks": 1, "existing_task_count": 5},
        {"coordination_policy": CoordinationPolicy(max_rounds=1)},
        {"maximum_tasks": 0, "existing_task_count": 6},
        {"maximum_tasks": 3, "existing_task_count": 6},
    ],
)
def test_original_next_round_and_remaining_task_capacity_cannot_be_reset(change):
    source = dispatch_source(**change)
    with pytest.raises(ValueError):
        compile_result(source, coordination_result(source, count=2))


@pytest.mark.parametrize(
    "tamper",
    [
        "result_root",
        "job_root",
        "result_source_hash",
        "result_content_hash",
        "budget_root",
        "budget_policy",
        "budget_start",
        "future_result",
        "future_source",
    ],
)
def test_compile_rejects_source_result_budget_and_time_mismatches(tamper):
    source = dispatch_source()
    result = coordination_result(source)
    budget = budget_snapshot(source)
    arguments = {}
    if tamper == "result_root":
        result = result.model_copy(update={"investigation_run_id": uuid4()})
    elif tamper == "job_root":
        result = result.model_copy(update={"job_id": uuid4()})
    elif tamper == "result_source_hash":
        result = result.model_copy(update={"source_sha256": "0" * 64})
    elif tamper == "result_content_hash":
        arguments["result_sha256"] = "0" * 64
    elif tamper == "budget_root":
        budget = budget.model_copy(update={"investigation_run_id": uuid4()})
    elif tamper == "budget_policy":
        budget = budget.model_copy(update={"policy": CallBudgetPolicy(max_model_calls=1)})
    elif tamper == "budget_start":
        budget = budget.model_copy(update={"started_at": source.started_at + timedelta(seconds=1)})
    elif tamper == "future_result":
        result = result.model_copy(update={"created_at": DISPATCH_AT + timedelta(seconds=1)})
    else:
        source = source.model_copy(update={"created_at": NOW + timedelta(seconds=2)})
        result = coordination_result(source)
    with pytest.raises(ValueError):
        compile_result(source, result, budget=budget, **arguments)


@pytest.mark.parametrize(
    "corruption",
    [
        "missing_deadline",
        "missing_policy",
        "missing_usage",
        "model_count",
        "tool_count",
        "sequence",
        "summary",
        "call_root",
    ],
)
def test_missing_or_inconsistent_ledger_cannot_create_queued_tasks(corruption):
    source = dispatch_source()
    call = model_call(source, build_packet(source))
    budget = budget_snapshot(source, (call,))
    changes = {
        "missing_deadline": {"deadline": None},
        "missing_policy": {"policy": None},
        "missing_usage": {"model_usage": None},
        "model_count": {"reserved_model_calls": 0},
        "tool_count": {"reserved_tool_calls": 1},
        "sequence": {"calls": (call.model_copy(update={"sequence": 2}),)},
        "summary": {
            "model_usage": budget.model_usage.model_copy(update={"committed_input_tokens": 0})
        },
        "call_root": {"calls": (call.model_copy(update={"investigation_run_id": uuid4()}),)},
    }[corruption]
    with pytest.raises(ValueError):
        compile_result(source, budget=budget.model_copy(update=changes))


@pytest.mark.parametrize(
    "dimension,code",
    [
        ("models", "model_call_budget_exceeded"),
        ("tools", "tool_call_budget_exceeded"),
        ("input", "run_input_budget_exceeded"),
        ("output", "run_output_budget_exceeded"),
        ("cost", "model_cost_budget_exceeded"),
    ],
)
def test_minimum_capacity_is_checked_for_every_task(dimension, code):
    source = dispatch_source()
    model = source.model_budget
    one_cost = estimate_cost(
        model.profile, model.max_input_tokens_per_call, model.max_output_tokens_per_call
    )
    if dimension == "models":
        source = source.model_copy(update={"call_budget": CallBudgetPolicy(max_model_calls=1)})
    elif dimension == "tools":
        source = source.model_copy(update={"call_budget": CallBudgetPolicy(max_tool_calls=1)})
    else:
        changes = {
            "input": {"max_input_tokens": model.max_input_tokens_per_call},
            "output": {"max_output_tokens": model.max_output_tokens_per_call},
            "cost": {"max_estimated_cost_usd": one_cost},
        }[dimension]
        source = source.model_copy(update={"model_budget": model.model_copy(update=changes)})
    assert capacity_blocker(budget_snapshot(source), 1, now=DISPATCH_AT) is None
    record = compile_result(source, coordination_result(source, count=2))
    assert record.status == "blocked"
    assert record.reason_code == code
    assert record.round_id is record.assignment is None


def test_unknown_usage_keeps_allocation_and_can_exhaust_next_task_capacity():
    source = dispatch_source()
    packet = build_packet(source)
    source = source.model_copy(
        update={
            "model_budget": source.model_budget.model_copy(
                update={"max_input_tokens": source.model_budget.max_input_tokens_per_call}
            )
        }
    )
    packet = build_packet(source)
    call = model_call(source, packet)
    budget = budget_snapshot(source, (call,))
    assert budget.model_usage.unknown_model_calls == 1
    assert budget.model_usage.reported_cost_usd is None
    assert budget.model_usage.committed_input_tokens == packet.allocation.estimated_input_tokens
    record = compile_result(source, budget=budget)
    assert record.status == "blocked"
    assert record.reason_code == "run_input_budget_exceeded"
    assert record.budget == budget


@pytest.mark.parametrize("overrun", ["model", "tool_total", "tool_task", "reported_usage"])
def test_already_overdrawn_original_ledger_is_blocked(overrun):
    source = dispatch_source()
    packet = build_packet(source)
    if overrun == "model":
        source = source.model_copy(update={"call_budget": CallBudgetPolicy(max_model_calls=1)})
        calls = (model_call(source, packet), model_call(source, packet, sequence=2))
        expected = "model_call_budget_exceeded"
    elif overrun.startswith("tool"):
        policy = (
            CallBudgetPolicy(max_tool_calls=1)
            if overrun == "tool_total"
            else (CallBudgetPolicy(max_tool_calls_per_task=1))
        )
        source = source.model_copy(update={"call_budget": policy})
        task = uuid4()
        calls = tuple(
            model_call(
                source,
                packet,
                sequence=i,
                kind="tool",
                operation=CallOperation.QUERY_LOGS,
                model_allocation=None,
                reinvestigation_attempt_id=None,
                task_id=task,
                task_attempt_id=uuid4(),
            )
            for i in (1, 2)
        )
        expected = "tool_call_budget_exceeded"
    else:
        inputs = packet.allocation.estimated_input_tokens + 1
        call = model_call(
            source,
            packet,
            model_usage=ReportedModelUsage(
                input_tokens=inputs, output_tokens=1, total_tokens=inputs + 1
            ),
        )
        calls = (call,)
        expected = "model_usage_exceeded_allocation"
    record = compile_result(source, budget=budget_snapshot(source, calls))
    assert record.status == "blocked"
    assert record.reason_code == expected


@pytest.mark.parametrize(
    "status,code", [("reserved", "calls_in_flight"), ("unknown", "run_deadline_exceeded")]
)
def test_inflight_calls_and_original_deadline_block_without_reset(status, code):
    source = dispatch_source()
    call = model_call(source, build_packet(source), status=status)
    now = (
        DISPATCH_AT
        if status == "reserved"
        else (source.started_at + timedelta(seconds=source.call_budget.max_duration_seconds))
    )
    record = compile_result(source, budget=budget_snapshot(source, (call,)), now=now)
    assert record.status == "blocked"
    assert record.reason_code == code


def test_reported_zero_usage_is_distinct_from_unknown_usage():
    source = dispatch_source()
    packet = build_packet(source)
    call = model_call(
        source,
        packet,
        status="succeeded",
        model_usage=ReportedModelUsage(input_tokens=0, output_tokens=0, total_tokens=0),
    )
    budget = budget_snapshot(source, (call,))
    assert budget.model_usage.unknown_model_calls == 0
    assert budget.model_usage.reported_cost_usd == Decimal("0")
    assert compile_result(source, budget=budget).status == "queued"
