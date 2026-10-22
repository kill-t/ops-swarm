"""纯路由矩阵：原轮次、反证、人工约束和共享预算均不能重置。"""

import json
from datetime import timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.model_contracts import ModelBudgetUsage
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_routing.policy import compile_human_routing
from opsswarm.investigation.review.contracts import HypothesisReviewProposal, ReviewModelResult
from opsswarm.investigation.synthesis.contracts import OpenHypothesisRecord
from opsswarm.investigation.tasks.errors import TaskFactConflict
from tests.unit.investigation.test_human_preparation import NOW, source_fixture
from tests.unit.investigation.test_review_routing import (
    hypothesis,
    insufficient_review,
    sufficient_review,
)


def source(*, constraints=False, round_number=1, task_count=3, counter=()):
    supplied = source_fixture()
    h = hypothesis(counter_evidence_ids=counter)
    record = OpenHypothesisRecord(
        id=supplied.review.open_hypothesis_id,
        investigation_run_id=supplied.handoff.investigation_run_id,
        round_id=supplied.review.round_id,
        investigation_attempt_id=supplied.handoff.investigation_attempt_id,
        source_call_id=uuid4(),
        version=h.version,
        status=h.status,
        hypothesis=h,
        content_sha256=content_hash(h),
        created_at=NOW,
    )
    message = supplied.prior_input.messages[1].model_copy(
        update={
            "content": json.dumps(
                {
                    "open_hypothesis": record.model_dump(mode="json"),
                    "evidence": [
                        {"evidence_id": item} for item in ("log-1", "metric-1", "change-1")
                    ],
                }
            )
        }
    )
    events = supplied.wait.events
    if not constraints:
        command = events[0].command
        command = command.model_copy(
            update={"items": tuple(i for i in command.items if i.kind != "constraint")}
        )
        events = (
            events[0].model_copy(
                update={"command": command, "command_sha256": content_hash(command)}
            ),
        )
    route = supplied.route.decision.model_copy(
        update={
            "round_number": round_number,
            "remaining_rounds": 3 - round_number,
            "existing_task_count": task_count,
            "remaining_task_slots": 9 - task_count,
        }
    )
    return supplied.model_copy(
        update={
            "model_budget": supplied.model_budget.model_copy(
                update={"max_output_tokens_per_call": 1024}
            ),
            "prior_input": supplied.prior_input.model_copy(
                update={"messages": (supplied.prior_input.messages[0], message)}
            ),
            "wait": supplied.wait.model_copy(update={"events": events}),
            "route": supplied.route.model_copy(update={"decision": route}),
        }
    )


def snapshot(supplied, *, models=1, tools=1, **usage):
    return CallBudgetSnapshot(
        investigation_run_id=supplied.handoff.investigation_run_id,
        policy=supplied.call_budget,
        model_budget=supplied.model_budget,
        started_at=supplied.started_at,
        deadline=supplied.started_at + timedelta(seconds=supplied.call_budget.max_duration_seconds),
        reserved_model_calls=models,
        reserved_tool_calls=tools,
        calls=(),
        model_usage=ModelBudgetUsage(
            **{
                "committed_input_tokens": 100,
                "committed_output_tokens": 100,
                "committed_cost_usd": Decimal("0.001"),
                "reported_input_tokens": 0,
                "reported_output_tokens": 0,
                "reported_cost_usd": None,
                "unknown_model_calls": 1,
                **usage,
            }
        ),
    )


def result(review=None):
    review = review or sufficient_review()
    return ReviewModelResult(
        proposal=HypothesisReviewProposal(**review.model_dump(exclude={"version", "status"}))
    )


@pytest.mark.parametrize("round_number", [1, 2, 3])
def test_candidate_intent_keeps_original_round_and_unknown_usage(round_number):
    supplied = source(round_number=round_number)
    budget = snapshot(supplied)
    routed = compile_human_routing(supplied, result(), budget, now=NOW)
    assert routed.route == "candidate_evaluation" and routed.dispatch_status == "not_dispatched"
    assert routed.base_decision.round_number == round_number
    assert routed.minimum_model_calls == 1 and routed.minimum_tool_calls == 0
    assert routed.budget == budget and routed.budget.model_usage.unknown_model_calls == 1
    assert routed.budget.model_usage.reported_cost_usd is None


@pytest.mark.parametrize(
    ("round_number", "task_count", "expected", "reason"),
    [
        (1, 3, "follow_up", "follow_up_capacity_available"),
        (2, 8, "follow_up", "follow_up_capacity_available"),
        (3, 8, "await_human", "round_limit_reached"),
        (2, 9, "await_human", "total_task_limit_exceeded"),
    ],
)
def test_follow_up_uses_original_limits(round_number, task_count, expected, reason):
    supplied = source(round_number=round_number, task_count=task_count)
    routed = compile_human_routing(
        supplied, result(insufficient_review()), snapshot(supplied), now=NOW
    )
    assert routed.route == expected and reason in routed.base_decision.reason_codes
    assert routed.minimum_model_calls == (3 if expected == "follow_up" else 0)
    assert routed.minimum_tool_calls == (1 if expected == "follow_up" else 0)


@pytest.mark.parametrize("objection", ["hypothesis", "review", "gap", "competing"])
def test_substantive_objections_keep_human_wait(objection):
    supplied = source(counter=("change-1",) if objection == "hypothesis" else ())
    changes = {
        "review": {"counter_evidence_ids": ("change-1",)},
        "gap": {"evidence_gaps": ("样本仍不完整",)},
        "competing": {"competing_explanations": ("缓存故障尚未排除",)},
    }.get(objection, {})
    routed = compile_human_routing(
        supplied, result(sufficient_review(**changes)), snapshot(supplied), now=NOW
    )
    assert routed.route == "await_human" and routed.blocking_reason_codes == ()


@pytest.mark.parametrize("direction", ["candidate", "follow_up"])
def test_constraints_cannot_be_dismissed_by_model(direction):
    supplied = source(constraints=True)
    proposed = result() if direction == "candidate" else result(insufficient_review())
    routed = compile_human_routing(supplied, proposed, snapshot(supplied), now=NOW)
    assert routed.route == "await_human"
    assert routed.blocking_reason_codes == ("human_constraints_unresolved",)
    item = routed.unresolved_constraints[0]
    assert item.event_id == supplied.wait.events[0].id and item.item_index == 1
    assert item.text == supplied.wait.events[0].command.items[1].text


@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("models", "model_call_budget_exceeded"),
        ("tools", "tool_call_budget_exceeded"),
        ("input", "run_input_budget_exceeded"),
        ("output", "run_output_budget_exceeded"),
        ("cost", "model_cost_budget_exceeded"),
        ("deadline", "run_deadline_exceeded"),
    ],
)
def test_exhausted_budget_is_wait_intent_not_failed_opinion(kind, expected):
    supplied = source()
    changes = {
        "input": {"committed_input_tokens": supplied.model_budget.max_input_tokens},
        "output": {"committed_output_tokens": supplied.model_budget.max_output_tokens},
        "cost": {"committed_cost_usd": supplied.model_budget.max_estimated_cost_usd},
    }.get(kind, {})
    budget = snapshot(
        supplied,
        models=supplied.call_budget.max_model_calls if kind == "models" else 1,
        tools=supplied.call_budget.max_tool_calls if kind == "tools" else 1,
        **changes,
    )
    routed = compile_human_routing(
        supplied,
        result(insufficient_review()),
        budget,
        now=budget.deadline if kind == "deadline" else NOW,
    )
    assert routed.route == "await_human" and expected in routed.blocking_reason_codes


def test_minimum_capacity_boundary_and_all_blockers_are_retained():
    supplied = source(constraints=True)
    budget = snapshot(
        supplied,
        models=10,
        tools=18,
        committed_input_tokens=supplied.model_budget.max_input_tokens,
        committed_output_tokens=supplied.model_budget.max_output_tokens,
        committed_cost_usd=supplied.model_budget.max_estimated_cost_usd,
    )
    routed = compile_human_routing(
        supplied, result(insufficient_review()), budget, now=budget.deadline
    )
    assert routed.blocking_reason_codes == (
        "human_constraints_unresolved",
        "run_deadline_exceeded",
        "model_call_budget_exceeded",
        "tool_call_budget_exceeded",
        "run_input_budget_exceeded",
        "run_output_budget_exceeded",
        "model_cost_budget_exceeded",
    )
    supplied = source()
    # 恰好能容纳三次必要模型调用及一次工具调用；这里只判断最低容量，不预留。
    budget = snapshot(supplied, models=9, tools=17)
    routed = compile_human_routing(supplied, result(insufficient_review()), budget, now=NOW)
    assert routed.route == "follow_up"


def test_foreign_budget_or_evidence_cannot_create_intent():
    supplied = source()
    budget = snapshot(supplied).model_copy(update={"investigation_run_id": uuid4()})
    with pytest.raises(ValueError, match="原预算"):
        compile_human_routing(supplied, result(), budget, now=NOW)
    with pytest.raises(TaskFactConflict):
        compile_human_routing(
            supplied,
            result(sufficient_review(accepted_supporting_evidence_ids=("human-event-uuid",))),
            snapshot(supplied),
            now=NOW,
        )
