"""有限路由、人工约束、原容量及未知预算的纯领域验收。"""

from datetime import timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.reinvestigation_review.context import build_packet
from opsswarm.investigation.reinvestigation_review.contracts import ReflowReviewResult
from opsswarm.investigation.reinvestigation_routing.policy import compile_route
from tests.unit.investigation.test_reflow_context_policy import (
    budget_snapshot,
    model_call,
    reflow_source,
)
from tests.unit.investigation.test_reinvestigation_review import proposal, source_fixture


def material_source(*, events=(), **changes):
    def root(**kwargs):
        source = reflow_source(**(kwargs | changes))
        source = source.model_copy(
            update={
                "maximum_tasks": max(
                    0,
                    min(
                        source.coordination_policy.max_tasks_per_round,
                        source.coordination_policy.max_total_tasks - source.existing_task_count,
                    ),
                )
            }
        )
        materials = []
        for m in source.materials:
            if m.kind == "human_inputs":
                content = {"events": list(events)}
                m = m.model_copy(
                    update={"content": content, "content_sha256": raw_content_hash(content)}
                )
            materials.append(m)
        return source.model_copy(update={"materials": tuple(materials)})

    with patch("tests.unit.investigation.test_reinvestigation_dispatch_policy.reflow_source", root):
        return source_fixture()


def result_for(source, direction="evaluate_for_routing", **changes):
    return ReflowReviewResult(
        investigation_run_id=source.investigation_run_id,
        job_id=source.investigation_run_id,
        source_call_id=uuid4(),
        source_sha256=content_hash(source),
        synthesis_result_sha256=source.synthesis_result_sha256,
        proposal=proposal(source, direction, **changes),
        created_at=source.created_at + timedelta(seconds=1),
    )


def compile_for(
    source, direction="evaluate_for_routing", *, review=None, budget=None, now=None, **kwargs
):
    review = review or result_for(source, direction)
    return compile_route(
        source,
        review,
        budget or budget_snapshot(source),
        now=now or review.created_at,
        basis_event_cursor=1,
        **kwargs,
    )


@pytest.mark.parametrize(
    "direction,route,models,tools",
    [
        ("evaluate_for_routing", "candidate_evaluation", 1, 0),
        ("follow_up", "follow_up", 3, 1),
        ("human_review", "await_human", 0, 0),
    ],
)
def test_three_intents_preserve_material_and_original_counts(direction, route, models, tools):
    source = material_source()
    record = compile_for(source, direction)
    assert record.route == route
    assert record.dispatch_status == "not_dispatched"
    assert (record.minimum_model_calls, record.minimum_tool_calls) == (models, tools)
    assert record.base_decision.round_number == source.next_round_number
    assert record.base_decision.existing_task_count == source.existing_task_count + len(
        source.professionals
    )
    assert record.unresolved_questions == source.synthesis_result.proposal.unresolved_questions
    assert (
        record.observation_windows[0].effective_window == source.professionals[0].effective_window
    )


@pytest.mark.parametrize("field", ["evidence_gaps", "competing_explanations"])
def test_sufficient_opinion_with_objections_cannot_enter_candidate(field):
    source = material_source()
    review = result_for(source, **{field: ("仍有实质异议",)})
    record = compile_for(source, review=review)
    assert record.route == "await_human"


@pytest.mark.parametrize(
    "changes,reason",
    [
        ({"max_rounds": 2}, "round_limit_reached"),
        ({"max_total_tasks": 4}, "total_task_limit_exceeded"),
    ],
)
def test_original_capacity_not_reset(changes, reason):
    from opsswarm.investigation.coordination.contracts import CoordinationPolicy

    source = material_source(coordination_policy=CoordinationPolicy(**changes))
    record = compile_for(source, "follow_up")
    assert record.route == "await_human" and reason in record.base_decision.reason_codes


def test_human_constraint_is_never_model_resolved():
    from tests.unit.investigation.test_reflow_context_policy import NOW

    identity = uuid4()
    command = {
        "action": "supplement",
        "handoff_id": str(uuid4()),
        "source_sha256": "a" * 64,
        "expected_wait_version": 1,
        "idempotency_key": "constraint-137",
        "reason": "保留操作限制",
        "items": [{"kind": "constraint", "text": "禁止再次发布", "observed_at": None}],
    }
    event = {
        "id": str(identity),
        "investigation_run_id": str(uuid4()),
        "handoff_id": command["handoff_id"],
        "state_version": 2,
        "action": "supplement",
        "subject_id": "engineer",
        "authorization_id": str(uuid4()),
        "command": command,
        "command_sha256": raw_content_hash(command),
        "created_at": NOW.isoformat(),
    }
    record = compile_for(material_source(events=(event,)))
    assert record.route == "await_human"
    assert record.blocking_reason_codes == ("human_constraints_unresolved",)
    assert record.unresolved_constraints[0].event_id == identity
    assert record.unresolved_constraints[0].text == "禁止再次发布"


@pytest.mark.parametrize(
    "field,value,code",
    [
        ("max_model_calls", 1, "model_call_budget_exceeded"),
        ("max_tool_calls", 1, "tool_call_budget_exceeded"),
    ],
)
def test_whole_followup_minimum_not_one_call(field, value, code):
    from opsswarm.investigation.budget.contracts import CallBudgetPolicy

    source = material_source(call_budget=CallBudgetPolicy(**{field: value}))
    budget = budget_snapshot(source)
    if field == "max_tool_calls":
        # 单专业请求只需一个工具；改为两专业建议来验证整套最低容量。
        review = result_for(
            source,
            "follow_up",
            follow_up_suggestions=(
                {"role": "log", "question": "日志缺口", "rationale": "核查异常"},
                {"role": "metric", "question": "指标缺口", "rationale": "核查异常"},
            ),
        )
    else:
        review = result_for(source, "follow_up")
    assert code in compile_for(source, review=review, budget=budget).blocking_reason_codes


def test_unknown_usage_retains_allocation_and_cannot_reset_budget():
    source = material_source()
    packet = build_packet(source)
    call = model_call(
        source,
        packet,
        operation=CallOperation.REVIEW_REINVESTIGATION,
        reinvestigation_attempt_id=None,
        reflow_review_attempt_id=uuid4(),
    )
    budget = budget_snapshot(source, (call,))
    record = compile_for(source, budget=budget)
    assert record.budget.model_usage.unknown_model_calls == 1
    assert (
        record.budget.model_usage.committed_input_tokens == packet.allocation.estimated_input_tokens
    )
    wrong = budget.model_copy(update={"reserved_model_calls": 0})
    with pytest.raises(ValueError):
        compile_for(source, budget=wrong)


def test_deadline_and_changed_basis_only_make_human_intent():
    source = material_source()
    deadline = budget_snapshot(source).deadline
    record = compile_for(source, now=deadline, basis_blocker="reflow_source_changed")
    assert record.route == "await_human"
    assert record.blocking_reason_codes[:2] == ("reflow_source_changed", "run_deadline_exceeded")


@pytest.mark.parametrize(
    "field,value",
    [("source_sha256", "0" * 64), ("synthesis_result_sha256", "0" * 64), ("job_id", uuid4())],
)
def test_forged_success_binding_rejected(field, value):
    source = material_source()
    with pytest.raises(ValueError):
        compile_for(source, review=result_for(source).model_copy(update={field: value}))


@pytest.mark.parametrize(
    "field,code",
    [
        ("max_input_tokens", "run_input_budget_exceeded"),
        ("max_output_tokens", "run_output_budget_exceeded"),
        ("max_estimated_cost_usd", "model_cost_budget_exceeded"),
    ],
)
def test_full_followup_input_output_and_cost_are_checked(field, code):
    from opsswarm.investigation.budget.model_policy import estimate_cost

    model = reflow_source().model_budget
    amount = {
        "max_input_tokens": model.max_input_tokens_per_call * 2,
        "max_output_tokens": model.max_output_tokens_per_call * 2,
        "max_estimated_cost_usd": estimate_cost(
            model.profile, model.max_input_tokens_per_call, model.max_output_tokens_per_call
        )
        * 2,
    }[field]
    source = material_source(model_budget=model.model_copy(update={field: amount}))
    record = compile_for(source, "follow_up")
    assert record.route == "await_human" and code in record.blocking_reason_codes


def test_inflight_ledger_is_not_frozen_as_history():
    source = material_source()
    call = model_call(
        source,
        build_packet(source),
        status="reserved",
        operation=CallOperation.REVIEW_REINVESTIGATION,
        reinvestigation_attempt_id=None,
        reflow_review_attempt_id=uuid4(),
    )
    with pytest.raises(ValueError):
        compile_for(source, budget=budget_snapshot(source, (call,)))
