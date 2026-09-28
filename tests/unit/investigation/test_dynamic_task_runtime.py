"""动态任务运行器的持久轮次边界测试。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.tasks.contracts import RoundStatus, TaskAssignment, TaskStatus
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.records import CollaborationSnapshot, RoundRecord, TaskRecord
from opsswarm.investigation.tasks.runtime import DynamicTaskRuntime

NOW = datetime(2026, 9, 28, tzinfo=UTC)


class _Queries:
    def __init__(self, snapshot: CollaborationSnapshot) -> None:
        self._snapshot = snapshot

    async def get_snapshot(self, run_id):
        assert run_id == self._snapshot.investigation_run_id
        return self._snapshot


def _runtime_with_second_round() -> tuple[DynamicTaskRuntime, RoundRecord, TaskRecord]:
    run_id, round_id = uuid4(), uuid4()
    assignment = TaskAssignment(
        task_key="metric",
        role=EvidenceKind.METRIC,
        goal="检查等待队列",
        service_name="checkout",
        window_start=NOW - timedelta(minutes=15),
        window_end=NOW,
        evidence_snapshot_id="scenario-runtime",
    )
    task = TaskRecord(
        id=uuid4(),
        investigation_run_id=run_id,
        round_id=round_id,
        assignment=assignment,
        status=TaskStatus.PENDING,
        version=1,
        attempt_count=0,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )
    round_record = RoundRecord(
        id=round_id,
        investigation_run_id=run_id,
        round_number=2,
        goal="补查证据缺口",
        status=RoundStatus.RUNNING,
        created_at=NOW,
        completed_at=None,
        tasks=(task,),
    )
    snapshot = CollaborationSnapshot(investigation_run_id=run_id, rounds=(round_record,))
    runtime = DynamicTaskRuntime.__new__(DynamicTaskRuntime)
    runtime.run_id = run_id
    runtime.snapshot_id = "scenario-runtime"
    runtime._queries = _Queries(snapshot)
    return runtime, round_record, task


@pytest.mark.asyncio
async def test_dynamic_runtime_matches_persisted_second_round_and_full_assignment() -> None:
    runtime, round_record, task = _runtime_with_second_round()

    assert await runtime._planned_task(round_record.id, 2, task.assignment) == task

    changed = task.assignment.model_copy(update={"goal": "另一项调查"})
    with pytest.raises(TaskFactConflict) as caught:
        await runtime._planned_task(round_record.id, 2, changed)
    assert caught.value.code == "task_assignment_mismatch"


@pytest.mark.asyncio
async def test_dynamic_runtime_rejects_round_number_mismatch() -> None:
    runtime, round_record, task = _runtime_with_second_round()

    with pytest.raises(TaskFactConflict) as caught:
        await runtime._planned_task(round_record.id, 1, task.assignment)

    assert caught.value.code == "task_round_mismatch"
