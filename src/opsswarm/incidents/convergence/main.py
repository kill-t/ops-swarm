"""独立收敛候选事故结果，不重新采样或执行外部动作。"""

import argparse
import asyncio
import signal
import sys
from contextlib import suppress

from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.incidents.convergence.store import CandidateIncidentConvergenceStore


async def run(*, once: bool) -> bool | None:
    database = Database(DatabaseSettings().database_url)
    store = CandidateIncidentConvergenceStore(database.session_factory)
    try:
        if once:
            return await store.converge_next()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            loop.add_signal_handler(signal.SIGTERM, stop.set)
            loop.add_signal_handler(signal.SIGINT, stop.set)
        while not stop.is_set():
            if not await store.converge_next():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=2)
        return None
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="候选处置结果的独立事故收敛")
    parser.add_argument("--once", action="store_true", help="至多原子收敛一个事故结果")
    args = parser.parse_args()
    with suppress(KeyboardInterrupt):
        asyncio.run(
            run(once=args.once),
            loop_factory=create_selector_event_loop if sys.platform == "win32" else None,
        )


if __name__ == "__main__":
    main()
