"""运行独立人工输入准备 Worker，不装配模型或执行环境。"""

import argparse
import asyncio
import signal
import sys
from contextlib import suppress
from uuid import uuid4

from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.investigation.human_prepare.job_store import HumanPreparationJobStore
from opsswarm.investigation.human_prepare.worker import HumanPreparationWorker


async def run(*, once: bool) -> bool | None:
    database = Database(DatabaseSettings().database_url)
    worker = HumanPreparationWorker(
        HumanPreparationJobStore(database.session_factory), worker_id=f"human-prepare-{uuid4().hex}"
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
    parser = argparse.ArgumentParser(description="运行可信人工输入准备 Worker（不调用模型）")
    parser.add_argument("--once", action="store_true", help="至多处理一个持久准备任务")
    args = parser.parse_args()
    loop_factory = create_selector_event_loop if sys.platform == "win32" else None
    with suppress(KeyboardInterrupt):
        result = asyncio.run(run(once=args.once), loop_factory=loop_factory)
        if args.once:
            print("已处理一个人工输入准备任务" if result else "当前没有人工输入准备任务")


if __name__ == "__main__":
    main()
