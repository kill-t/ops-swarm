"""供集成测试启动独立 Worker 进程，连接测试专用业务与 checkpoint 数据库。"""

import asyncio
import hashlib
import json
import sys
from datetime import timedelta

from opsswarm.database import Database
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.investigation.tasks.queries import CollaborationQueries
from tests.checkpoint_fakes import CountingDataSource, SpecialistAnalystStub
from tests.integration.test_budgeted_graph import run_worker
from tests.integration.test_fixed_task_graph import FailLogsAfterCommittedPeers
from tests.task_facts_helpers import BASE_TIME, TaskClock


class ProcessSource(CountingDataSource):
    def __init__(self, *, legacy):
        super().__init__()
        self.legacy = legacy

    @property
    def snapshot_id(self):
        if self.legacy:
            # 精确保留修复前的身份算法，证明升级后可恢复已有旧身份事实。
            return "frozen:" + hashlib.sha256(self._scenario.model_dump_json().encode()).hexdigest()
        return super().snapshot_id


async def run(payload):
    from uuid import UUID

    db = Database(payload["database_url"])
    try:
        run_id = UUID(payload["run_id"])
        clock = TaskClock(BASE_TIME + timedelta(seconds=payload["clock_seconds"]))
        source = ProcessSource(legacy=payload["legacy"])
        analyst = (
            FailLogsAfterCommittedPeers(CollaborationQueries(db.session_factory), run_id)
            if payload["fail"]
            else SpecialistAnalystStub()
        )
        store = InvestigationStore(db.session_factory)
        processed = await run_worker(
            db.session_factory, payload["checkpoint_url"], store, clock, source, analyst
        )
        result = await store.get_run(run_id)
        return {
            "processed": processed,
            "status": result.status.value,
            "source_id": source.snapshot_id,
            "queries": {kind.value: count for kind, count in source.calls.items()},
        }
    finally:
        await db.dispose()


if __name__ == "__main__":
    payload = json.loads(sys.stdin.read())
    factory = create_selector_event_loop if sys.platform == "win32" else None
    print(json.dumps(asyncio.run(run(payload), loop_factory=factory)))
