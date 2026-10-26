"""恢复窗口继承原总期限，不因延迟启动或新尝试重新计时。"""

from datetime import UTC, datetime, timedelta

import pytest

from opsswarm.execution.release_jobs.contracts import ExecutionJobRecord, ExecutionJobView
from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.verification.candidate.contracts import RecoveryError
from opsswarm.verification.candidate.source import fixed_window

NOW = datetime(2026, 10, 26, tzinfo=UTC)


@pytest.mark.parametrize("remaining", [-30, 0, 60, 120, 300])
def test_window_inherits_original_deadline_even_when_already_expired(remaining):
    run = InvestigationRunRow(
        started_at=NOW + timedelta(seconds=remaining - 300),
        call_budget=CallBudgetPolicy(max_duration_seconds=300).model_dump(mode="json"),
    )
    source = ExecutionJobView.model_construct(
        record=ExecutionJobRecord.model_construct(completed_at=NOW),
        attempts=(),
        result=None,
    )
    assert fixed_window(run, source) == (
        NOW,
        NOW + timedelta(seconds=min(remaining, 120)),
    )


@pytest.mark.parametrize("field", ["started_at", "call_budget"])
def test_missing_original_budget_cannot_grant_new_window(field):
    run = InvestigationRunRow(
        started_at=NOW,
        call_budget=CallBudgetPolicy().model_dump(mode="json"),
    )
    setattr(run, field, None)
    source = ExecutionJobView.model_construct(
        record=ExecutionJobRecord.model_construct(completed_at=NOW),
        attempts=(),
        result=None,
    )
    with pytest.raises(RecoveryError, match="原运行缺少固定预算"):
        fixed_window(run, source)
