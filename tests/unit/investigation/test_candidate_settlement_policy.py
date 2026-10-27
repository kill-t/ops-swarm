"""父收尾区分健康反证、未知结果及后续意图，不制造业务成功。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from opsswarm.investigation.settlement.contracts import SettlementError
from opsswarm.investigation.settlement.policy import disposition, require_execution_binding
from opsswarm.verification.candidate.contracts import RecoveryResultContent


def result(outcome, reason):
    return RecoveryResultContent(
        execution_job_id=uuid4(),
        execution_result_sha256="a" * 64,
        operation_id=uuid4(),
        outcome=outcome,
        attempt_id=None,
        reason_code=reason,
        created_at=datetime(2026, 10, 27, tzinfo=UTC),
        parent_advanced=False,
    )


@pytest.mark.parametrize(
    ("outcome", "reason", "expected"),
    [
        ("recovered", "business_probe_passed", "recovery_verified"),
        ("not_recovered", "business_probe_failed", "reinvestigation_required"),
        ("not_recovered", "execution_superseded", "human_resolution_required"),
        ("source_failed", "probe_source_failed", "human_resolution_required"),
        ("source_failed", "recovery_exhausted", "human_resolution_required"),
        ("execution_unknown", "execution_unknown", "human_resolution_required"),
        ("not_executed", "execution_not_applied", "human_resolution_required"),
    ],
)
def test_each_health_or_uncertainty_fact_has_explicit_followup_intent(outcome, reason, expected):
    assert disposition(result(outcome, reason)) == expected


@pytest.mark.parametrize(
    "outcome", ["recovered", "not_recovered", "source_failed", "execution_unknown", "not_executed"]
)
def test_unrecognized_reason_cannot_silently_become_human_resolution(outcome):
    with pytest.raises(SettlementError):
        disposition(result(outcome, "unrecognized_reason"))


@pytest.mark.parametrize(
    ("execution", "outcome", "reason"),
    [
        ("unknown", "execution_unknown", "execution_unknown"),
        ("blocked", "not_executed", "execution_not_applied"),
        ("not_applied", "not_executed", "execution_not_applied"),
        ("applied_superseded", "not_recovered", "execution_superseded"),
        ("applied_current", "recovered", "business_probe_passed"),
        ("applied_current", "not_recovered", "business_probe_failed"),
        ("applied_current", "source_failed", "recovery_exhausted"),
    ],
)
def test_parent_leaf_preserves_exact_execution_and_recovery_scope(execution, outcome, reason):
    require_execution_binding(execution, result(outcome, reason))


@pytest.mark.parametrize(
    ("execution", "outcome", "reason"),
    [
        ("unknown", "recovered", "business_probe_passed"),
        ("not_applied", "recovered", "business_probe_passed"),
        ("blocked", "source_failed", "probe_source_failed"),
        ("applied_superseded", "not_recovered", "business_probe_failed"),
        ("applied_current", "not_recovered", "execution_superseded"),
        ("applied_current", "execution_unknown", "execution_unknown"),
    ],
)
def test_scope_conflicts_do_not_unlock_a_settled_parent(execution, outcome, reason):
    with pytest.raises(SettlementError):
        require_execution_binding(execution, result(outcome, reason))
