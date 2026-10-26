"""启动候选发布后的独立只读恢复验证Worker。"""

import argparse
import asyncio
import signal
import sys
from contextlib import suppress
from uuid import uuid4

from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.verification.candidate.probe import Probe
from opsswarm.verification.candidate.store import CandidateRecoveryStore
from opsswarm.verification.candidate.worker import CandidateRecoveryWorker


async def run(*, once: bool) -> bool | None:
    database = Database(DatabaseSettings().database_url)
    worker = CandidateRecoveryWorker(
        CandidateRecoveryStore(database.session_factory),
        probe=Probe(),
        worker_id=f"recovery-{uuid4().hex}",
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
    parser = argparse.ArgumentParser(description="候选发布后的只读业务恢复验证Worker")
    parser.add_argument("--once", action="store_true", help="扫描已结算执行并至多采样一个任务")
    args = parser.parse_args()
    with suppress(KeyboardInterrupt):
        asyncio.run(
            run(once=args.once),
            loop_factory=create_selector_event_loop if sys.platform == "win32" else None,
        )


if __name__ == "__main__":
    main()
