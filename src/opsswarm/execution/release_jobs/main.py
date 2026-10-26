"""启动独立可信候选发布Worker；不在HTTP请求内执行远端操作。"""

import argparse
import asyncio
import signal
import sys
from contextlib import suppress
from uuid import uuid4

from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.execution.release_jobs.client import ReleaseDispatchClient
from opsswarm.execution.release_jobs.job_store import ReleaseExecutionJobStore
from opsswarm.execution.release_jobs.worker import ReleaseExecutionWorker


async def run(*, once: bool) -> bool | None:
    database = Database(DatabaseSettings().database_url)
    worker = ReleaseExecutionWorker(
        ReleaseExecutionJobStore(database.session_factory),
        client=ReleaseDispatchClient(),
        worker_id=f"release-{uuid4().hex}",
    )
    try:
        if once:
            return await worker.run_once()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            loop.add_signal_handler(signal.SIGTERM, stop.set)
            loop.add_signal_handler(signal.SIGINT, stop.set)
        await worker.run_forever(stop)
        return None
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="运行候选发布执行Worker（仅隔离演练）")
    parser.add_argument("--once", action="store_true", help="至多处理一个持久执行任务")
    args = parser.parse_args()
    with suppress(KeyboardInterrupt):
        result = asyncio.run(
            run(once=args.once),
            loop_factory=create_selector_event_loop if sys.platform == "win32" else None,
        )
        if args.once:
            print("已处理一个执行任务" if result else "当前没有执行任务")


if __name__ == "__main__":
    main()
