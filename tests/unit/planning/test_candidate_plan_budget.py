"""候选规划复用原累计预算，不把未知用量或已到上限视为新额度。"""

from datetime import timedelta
from uuid import uuid4

import pytest

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.candidate.context import build_packet
from opsswarm.planning.candidate.budget import check_planning_budget
from opsswarm.planning.candidate.contracts import PlanError
from tests.unit.investigation.test_candidate_evaluation import candidate_source
from tests.unit.investigation.test_human_preparation import NOW, model_call, snapshot


def budget_fixture(*, models=1, tools=2, task_tools=2):
    source = candidate_source()
    source = source.model_copy(
        update={
            "call_budget": source.call_budget.model_copy(
                update={
                    "max_model_calls": models,
                    "max_tool_calls": tools,
                    "max_tool_calls_per_task": task_tools,
                }
            )
        }
    )
    return source, build_packet(source)


def with_tool_calls(source, *, task_ids=(), model_count=1):
    models = tuple(model_call(source) for _ in range(model_count))
    calls = (
        *models,
        *(
            model_call(source, status="succeeded").model_copy(
                update={
                    "kind": "tool",
                    "operation": CallOperation.QUERY_LOGS,
                    "task_id": task_id,
                    "model_allocation": None,
                    "model_usage": None,
                }
            )
            for task_id in task_ids
        ),
    )
    return snapshot(source, calls=models).model_copy(
        update={
            "calls": calls,
            "reserved_model_calls": len(models),
            "reserved_tool_calls": len(task_ids),
        }
    )


def test_exact_model_tool_and_task_limits_allow_deterministic_plan_without_new_reservation():
    source, packet = budget_fixture()
    task_id = uuid4()
    budget = with_tool_calls(source, task_ids=(task_id, task_id))
    before = budget.model_dump(mode="json")
    check_planning_budget(source, packet, budget, now=NOW)
    assert budget.model_dump(mode="json") == before


@pytest.mark.parametrize("limit", ["model", "tool", "task_tool"])
def test_accumulated_call_counts_above_any_original_limit_are_rejected(limit):
    source, packet = budget_fixture(tools=3 if limit == "task_tool" else 2)
    task_id = uuid4()
    budget = with_tool_calls(
        source,
        model_count=2 if limit == "model" else 1,
        task_ids=(task_id, task_id, task_id) if limit in {"tool", "task_tool"} else (),
    )
    with pytest.raises(PlanError) as caught:
        check_planning_budget(source, packet, budget, now=NOW)
    assert caught.value.code == "candidate_plan_call_limit_exceeded"


def test_task_tool_counts_are_scoped_to_each_task_not_the_whole_run():
    source, packet = budget_fixture(tools=3)
    first, second = uuid4(), uuid4()
    budget = with_tool_calls(source, task_ids=(first, first, second))
    check_planning_budget(source, packet, budget, now=NOW)


@pytest.mark.parametrize("field", ["reserved_model_calls", "reserved_tool_calls"])
@pytest.mark.parametrize("value", [None, 0, 2])
def test_unknown_or_mismatching_snapshot_counts_are_not_treated_as_zero(field, value):
    source, packet = budget_fixture()
    budget = with_tool_calls(source, task_ids=(uuid4(),)).model_copy(update={field: value})
    with pytest.raises(PlanError) as caught:
        check_planning_budget(source, packet, budget, now=NOW)
    assert caught.value.code == "candidate_plan_budget_invalid"


def test_unknown_counts_are_rejected_even_when_ledger_is_empty():
    source, packet = budget_fixture()
    budget = snapshot(source).model_copy(update={"reserved_model_calls": None})
    with pytest.raises(PlanError) as caught:
        check_planning_budget(source, packet, budget, now=NOW)
    assert caught.value.code == "candidate_plan_budget_invalid"


def test_unknown_model_usage_keeps_original_conservative_allocation_without_mutation():
    source, packet = budget_fixture()
    call = model_call(source, usage=None)
    budget = snapshot(source, calls=(call,))
    assert budget.model_usage is not None
    assert call.model_allocation is not None
    before = budget.model_dump(mode="json")
    check_planning_budget(source, packet, budget, now=NOW)
    assert budget.model_usage.unknown_model_calls == 1
    assert budget.model_usage.reported_cost_usd is None
    assert budget.model_usage.committed_input_tokens == call.model_allocation.estimated_input_tokens
    assert budget.model_usage.committed_output_tokens == call.model_allocation.output_token_limit
    assert budget.model_usage.committed_cost_usd == call.model_allocation.reserved_cost_usd
    assert budget.model_dump(mode="json") == before


def test_unknown_usage_that_exceeds_original_token_budget_still_blocks_planning():
    source, _ = budget_fixture()
    call = model_call(source)
    assert call.model_allocation is not None
    source = source.model_copy(
        update={
            "model_budget": source.model_budget.model_copy(
                update={"max_input_tokens": call.model_allocation.estimated_input_tokens - 1}
            )
        }
    )
    packet = build_packet(source)
    budget = snapshot(source, calls=(call,))
    with pytest.raises(PlanError) as caught:
        check_planning_budget(source, packet, budget, now=NOW)
    assert caught.value.code == "run_input_budget_exceeded"
    assert budget.calls[0].model_usage is None


@pytest.mark.parametrize("elapsed", [0, 1])
def test_original_deadline_blocks_planning_at_or_after_boundary(elapsed):
    source, packet = budget_fixture()
    deadline = source.started_at + timedelta(seconds=source.call_budget.max_duration_seconds)
    budget = snapshot(source, calls=(model_call(source),))
    with pytest.raises(PlanError) as caught:
        check_planning_budget(source, packet, budget, now=deadline + timedelta(seconds=elapsed))
    assert caught.value.code == "run_deadline_exceeded"
    assert budget.started_at == source.started_at
