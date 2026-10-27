"""回流准入只继承原账本，边界容量与损坏来源分别阻断。"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest

from opsswarm.incidents.convergence.contracts import ConvergenceRecord
from opsswarm.incidents.models import IncidentStatus
from opsswarm.investigation.budget.contracts import (
    CallBudgetPolicy,
    CallBudgetSnapshot,
    CallOperation,
    CallReservationRecord,
)
from opsswarm.investigation.budget.model_contracts import ModelAllocation, ReportedModelUsage
from opsswarm.investigation.budget.model_policy import estimate_cost, summarize_model_usage
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.reinvestigation.contracts import (
    ReinvestigationCommand,
    ReinvestigationError,
)
from opsswarm.investigation.reinvestigation.policy import budget_blockers, make_content, request_id
from tests.model_budget_helpers import model_policy

NOW = datetime(2026, 10, 27, tzinfo=UTC)
RUN_ID, INCIDENT_ID = uuid4(), uuid4()


def allocation():
    model = model_policy()
    return ModelAllocation(
        prompt_version="test-v1",
        input_sha256="a" * 64,
        input_bytes=744,
        estimated_input_tokens=1000,
        output_token_limit=model.max_output_tokens_per_call,
        reserved_cost_usd=estimate_cost(model.profile, 1000, model.max_output_tokens_per_call),
    )


def call(*, kind="model", **changes):
    return CallReservationRecord(
        **{
            "id": uuid4(),
            "investigation_run_id": RUN_ID,
            "investigation_attempt_id": uuid4(),
            "task_id": None,
            "task_attempt_id": None,
            "sequence": 1,
            "operation": CallOperation.COORDINATE if kind == "model" else CallOperation.QUERY_LOGS,
            "kind": kind,
            "status": "unknown",
            "error_code": None,
            "reserved_at": NOW - timedelta(seconds=5),
            "finished_at": NOW - timedelta(seconds=1),
            "model_allocation": allocation() if kind == "model" else None,
            **changes,
        }
    )


def snapshot(calls=(), *, model=None, policy=None, **changes):
    model = model or model_policy()
    return CallBudgetSnapshot(
        **{
            "investigation_run_id": RUN_ID,
            "policy": policy or CallBudgetPolicy(),
            "started_at": NOW - timedelta(seconds=10),
            "deadline": NOW + timedelta(seconds=290),
            "reserved_model_calls": sum(c.kind == "model" for c in calls),
            "reserved_tool_calls": sum(c.kind == "tool" for c in calls),
            "calls": calls,
            "model_budget": model,
            "model_usage": summarize_model_usage(
                model,
                [(c.model_allocation, c.model_usage) for c in calls if c.model_allocation],
            ),
            **changes,
        }
    )


def source(**changes):
    return ConvergenceRecord(
        **{
            "investigation_run_id": RUN_ID,
            "incident_id": INCIDENT_ID,
            "settlement_content_sha256": "a" * 64,
            "content_sha256": "b" * 64,
            "before_status": IncidentStatus.MITIGATING,
            "before_version": 3,
            "after_status": IncidentStatus.INVESTIGATING,
            "after_version": 3,
            "outcome": "observed_failure",
            "reason_code": "fresh_verification_failed",
            "fresh_until": NOW + timedelta(seconds=100),
            "latest_alert_id": None,
            "latest_alert_ingested_at": None,
            "actions": (),
            "slot_released": False,
            "created_at": NOW - timedelta(seconds=2),
            **changes,
        }
    )


def command(**changes):
    return ReinvestigationCommand(
        **{
            "convergence_sha256": "b" * 64,
            "expected_incident_version": 3,
            "idempotency_key": "reflow-test-1",
            "reason": "根据失败验证补查",
            **changes,
        }
    )


def make(**changes):
    return make_content(
        **{
            "run_id": RUN_ID,
            "incident_id": INCIDENT_ID,
            "convergence": source(),
            "budget": snapshot(),
            "command": command(),
            "subject_id": "engineer",
            "authorization_id": uuid4(),
            "now": NOW,
            **changes,
        }
    )


def test_content_preserves_original_budget_and_single_root_relationships():
    budget = snapshot((call(),))
    content = make(budget=budget)
    assert content.budget == budget
    assert content.budget.model_usage.unknown_model_calls == 1
    assert content.budget.model_usage.reported_cost_usd is None
    assert content.id == request_id(RUN_ID) == RUN_ID
    assert content.root_run_id == content.previous_run_id == content.budget_run_id == RUN_ID
    assert content.max_reflows == content.reflow_number == 1
    assert content.dispatch_status == "not_dispatched"
    assert content.command_sha256 == content_hash(command())


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("max_input_tokens", "run_input_budget_exceeded"),
        ("max_output_tokens", "run_output_budget_exceeded"),
        ("max_estimated_cost_usd", "model_cost_budget_exceeded"),
    ],
)
def test_complete_call_capacity_exact_boundary_and_one_unit_short(field, reason):
    original = snapshot((call(),))
    model, usage = original.model_budget, original.model_usage
    limits = {
        "max_input_tokens": usage.committed_input_tokens + model.max_input_tokens_per_call,
        "max_output_tokens": usage.committed_output_tokens + model.max_output_tokens_per_call,
        "max_estimated_cost_usd": usage.committed_cost_usd
        + estimate_cost(
            model.profile, model.max_input_tokens_per_call, model.max_output_tokens_per_call
        ),
    }
    exact_model = model.model_copy(update={field: limits[field]})
    assert budget_blockers(snapshot(original.calls, model=exact_model), now=NOW) == ()
    unit = Decimal("0.000001") if field == "max_estimated_cost_usd" else 1
    short_model = model.model_copy(update={field: limits[field] - unit})
    assert budget_blockers(snapshot(original.calls, model=short_model), now=NOW) == (reason,)


def test_model_count_tool_count_and_deadline_boundaries():
    model = call()
    assert budget_blockers(
        snapshot((model,), policy=CallBudgetPolicy(max_model_calls=1)), now=NOW
    ) == ("model_call_budget_exceeded",)
    tools = (call(kind="tool"), call(kind="tool", sequence=2))
    assert (
        budget_blockers(snapshot(tools[:1], policy=CallBudgetPolicy(max_tool_calls=1)), now=NOW)
        == ()
    )
    assert budget_blockers(snapshot(tools, policy=CallBudgetPolicy(max_tool_calls=1)), now=NOW) == (
        "tool_call_budget_exceeded",
    )
    budget = snapshot()
    assert budget_blockers(budget, now=budget.deadline) == ("run_deadline_exceeded",)
    assert budget_blockers(budget, now=budget.deadline - timedelta(microseconds=1)) == ()


def test_in_flight_and_actual_overrun_block_without_zeroing_unknown_usage():
    assert budget_blockers(snapshot((call(status="reserved", finished_at=None),)), now=NOW) == (
        "calls_in_flight",
    )
    for usage in (
        ReportedModelUsage(input_tokens=1001, output_tokens=1, total_tokens=1002),
        ReportedModelUsage(input_tokens=1, output_tokens=4097, total_tokens=4098),
    ):
        assert budget_blockers(snapshot((call(model_usage=usage),)), now=NOW) == (
            "model_usage_exceeded_allocation",
        )
    small = ReportedModelUsage(input_tokens=1, output_tokens=1, total_tokens=2)
    budget = snapshot((call(model_usage=small),))
    assert budget.model_usage.committed_input_tokens == 1000
    assert budget_blockers(budget, now=NOW) == ()


@pytest.mark.parametrize(
    "changed",
    [
        {"policy": None},
        {"model_budget": None},
        {"model_usage": None},
        {"started_at": None},
        {"deadline": None},
        {"deadline": NOW + timedelta(seconds=291)},
        {"started_at": NOW.replace(tzinfo=None)},
        {"reserved_model_calls": 1},
        {"reserved_tool_calls": None},
    ],
)
def test_incomplete_or_forged_snapshot_is_integrity_error(changed):
    with pytest.raises(ReinvestigationError) as error:
        budget_blockers(snapshot().model_copy(update=changed), now=NOW)
    assert error.value.code == "reinvestigation_integrity_violation"


@pytest.mark.parametrize(
    "changed",
    [
        {"sequence": 2},
        {"investigation_run_id": uuid4()},
        {"kind": "tool"},
        {"model_allocation": None},
        {"finished_at": None},
        {"reserved_at": NOW + timedelta(seconds=1)},
        {"reserved_at": NOW - timedelta(seconds=20)},
        {"reserved_at": NOW.replace(tzinfo=None)},
        {"finished_at": NOW + timedelta(seconds=1)},
        {"finished_at": NOW.replace(tzinfo=None)},
        {"finished_at": NOW - timedelta(seconds=6)},
        {"status": "reserved"},
    ],
)
def test_call_integrity_is_validated(changed):
    item = call().model_copy(update=changed)
    with pytest.raises(ReinvestigationError) as error:
        budget_blockers(snapshot((item,)), now=NOW)
    assert error.value.code == "reinvestigation_integrity_violation"


@pytest.mark.parametrize(
    "changed",
    [
        {"estimated_input_tokens": 1001},
        {"estimated_input_tokens": 25000, "input_bytes": 24744},
        {"output_token_limit": 4095},
        {"reserved_cost_usd": Decimal("0")},
    ],
)
def test_forged_allocation_is_rejected_even_with_recomputed_summary(changed):
    item = call(model_allocation=allocation().model_copy(update=changed))
    with pytest.raises(ReinvestigationError) as error:
        budget_blockers(snapshot((item,)), now=NOW)
    assert error.value.code == "reinvestigation_integrity_violation"


def test_duplicate_ids_forged_summary_and_tool_usage_are_rejected():
    first = call()
    bad = [
        snapshot((first, first.model_copy(update={"sequence": 2}))),
        snapshot((first,), model_usage=summarize_model_usage(model_policy(), [])),
        snapshot(
            (
                call(
                    kind="tool",
                    model_usage=ReportedModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
                ),
            )
        ),
    ]
    for budget in bad:
        with pytest.raises(ReinvestigationError) as error:
            budget_blockers(budget, now=NOW)
        assert error.value.code == "reinvestigation_integrity_violation"


@pytest.mark.parametrize(
    "changed",
    [
        {"outcome": "recovered"},
        {"outcome": "held_uncertain"},
        {"slot_released": True},
        {"after_status": IncidentStatus.RESOLVED},
    ],
)
def test_only_observed_failure_with_held_slot_is_eligible(changed):
    with pytest.raises(ReinvestigationError) as error:
        make(convergence=source(**changed))
    assert error.value.code == "reinvestigation_source_not_eligible"


@pytest.mark.parametrize(
    "changed",
    [{"convergence_sha256": "c" * 64}, {"expected_incident_version": 4}],
)
def test_command_requires_exact_source_hash_and_version(changed):
    with pytest.raises(ReinvestigationError) as error:
        make(command=command(**changed))
    assert error.value.code == "reinvestigation_basis_changed"


@pytest.mark.parametrize(
    "changed",
    [
        {"run_id": uuid4()},
        {"incident_id": uuid4()},
        {"budget": snapshot(investigation_run_id=uuid4())},
        {"convergence": source(created_at=NOW + timedelta(seconds=1))},
        {"now": NOW.replace(tzinfo=None)},
    ],
)
def test_cross_source_or_future_convergence_is_rejected(changed):
    with pytest.raises(ReinvestigationError) as error:
        make(**changed)
    assert error.value.code == "reinvestigation_integrity_violation"


def test_make_checks_budget_again_and_raises_first_stable_reason():
    with pytest.raises(ReinvestigationError) as error:
        make(now=NOW + timedelta(seconds=300))
    assert error.value.code == "run_deadline_exceeded"


def test_naive_or_earlier_clock_and_reserved_usage_are_integrity_errors():
    for now in (NOW.replace(tzinfo=None), NOW - timedelta(seconds=11)):
        with pytest.raises(ReinvestigationError) as error:
            budget_blockers(snapshot(), now=now)
        assert error.value.code == "reinvestigation_integrity_violation"
    item = call(
        status="reserved",
        finished_at=None,
        model_usage=ReportedModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
    )
    with pytest.raises(ReinvestigationError) as error:
        budget_blockers(snapshot((item,)), now=NOW)
    assert error.value.code == "reinvestigation_integrity_violation"


def test_all_blockers_have_stable_order_and_bounded_size():
    usage = ReportedModelUsage(input_tokens=1001, output_tokens=4097, total_tokens=5098)
    calls = (
        call(model_usage=usage),
        call(sequence=2, status="reserved", finished_at=None),
        call(kind="tool", sequence=3),
        call(kind="tool", sequence=4),
    )
    model = model_policy(
        max_input_tokens=1, max_output_tokens=1, max_estimated_cost_usd=Decimal("0.000001")
    )
    budget = snapshot(
        calls, model=model, policy=CallBudgetPolicy(max_model_calls=1, max_tool_calls=1)
    )
    reasons = budget_blockers(budget, now=budget.deadline)
    assert reasons == (
        "run_deadline_exceeded",
        "calls_in_flight",
        "model_call_budget_exceeded",
        "tool_call_budget_exceeded",
        "run_input_budget_exceeded",
        "run_output_budget_exceeded",
        "model_cost_budget_exceeded",
        "model_usage_exceeded_allocation",
    )
    assert len(reasons) <= 12


@pytest.mark.parametrize("version", [True, 1.0, 0, 2147483648])
def test_command_version_requires_strict_postgresql_integer(version):
    with pytest.raises(ValueError):
        command(expected_incident_version=version)


def test_tool_capacity_preserves_original_per_task_limit():
    task = uuid4()
    calls = (call(kind="tool", task_id=task), call(kind="tool", task_id=task, sequence=2))
    policy = CallBudgetPolicy(max_tool_calls=10, max_tool_calls_per_task=1)
    assert budget_blockers(snapshot(calls, policy=policy), now=NOW) == (
        "tool_call_budget_exceeded",
    )
