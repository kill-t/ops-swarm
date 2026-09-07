"""不同哈希种子的独立 Worker 进程共用真实数据库恢复新旧冻结任务。"""

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import update

from opsswarm.checkpoints import initialize_checkpoint_schema
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from tests.task_facts_helpers import started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
ROOT = Path(__file__).resolve().parents[2]


async def worker_process(payload, *, seed):
    # Windows 的数据库 Selector 循环不支持异步子进程，等待放在线程内。
    result = await asyncio.to_thread(
        subprocess.run,
        [sys.executable, "-m", "tests.frozen_worker_process"],
        cwd=ROOT,
        env={**os.environ, "PYTHONHASHSEED": str(seed)},
        input=json.dumps(payload).encode(),
        capture_output=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")[-2000:]
    return json.loads(result.stdout)


@pytest.mark.parametrize("legacy,snapshots", [(False, True), (True, True), (True, False)])
async def test_new_process_resumes_same_persisted_identity(
    migrated_database_url, database_session_factory, checkpoint_database_url, legacy, snapshots
):
    db = database_session_factory
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(db, workflow_version=WorkflowVersion.V2)
    if not snapshots:
        async with db() as session, session.begin():
            await session.execute(
                update(InvestigationRunRow)
                .where(InvestigationRunRow.id == started.run.id)
                .values(evidence_policy_version=None)
            )
    payload = {
        "database_url": migrated_database_url,
        "checkpoint_url": checkpoint_database_url,
        "run_id": str(started.run.id),
        "clock_seconds": 2,
        "legacy": legacy,
        "fail": True,
    }
    first = await worker_process(payload, seed=1)
    assert first["processed"] and first["status"] == "queued"
    queries = CollaborationQueries(db)
    before = (await queries.get_snapshot(started.run.id)).rounds[0]
    assert {t.assignment.evidence_snapshot_id for t in before.tasks} == {first["source_id"]}
    assert first["source_id"].startswith("frozen:" if legacy else "frozen-v2:")
    evidence = [await queries.get_evidence(started.run.id, t.id) for t in before.tasks]
    reports = [await queries.get_result(started.run.id, t.id) for t in before.tasks]
    second = await worker_process(
        {**payload, "clock_seconds": 4, "legacy": False, "fail": False}, seed=2
    )
    assert second["processed"] and second["status"] == "succeeded"
    assert second["queries"] == {"log": 0 if snapshots else 1, "metric": 0, "change": 0}
    after = (await queries.get_snapshot(started.run.id)).rounds[0]
    assert [t.id for t in after.tasks] == [t.id for t in before.tasks]
    assert [t.assignment for t in after.tasks] == [t.assignment for t in before.tasks]
    assert [t.attempt_count for t in after.tasks] == [1, 2, 1]
    assert evidence == [await queries.get_evidence(started.run.id, t.id) for t in after.tasks]
    for old, task in zip(reports, after.tasks, strict=True):
        saved = await queries.get_result(started.run.id, task.id)
        assert saved.result.evidence_snapshot_id == first["source_id"]
        if old is not None:
            assert saved == old
    budget = await CallBudgetQueries(db).get_snapshot(started.run.id)
    assert budget.reserved_tool_calls == (3 if snapshots else 4)
    assert budget.reserved_model_calls == 5
    assert (await store.get_run(started.run.id)).status == "succeeded"
