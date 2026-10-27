"""独立扫描已验证候选并原子收尾，不重开图或执行网络请求。"""

import argparse
import asyncio
import signal
import sys
from contextlib import suppress

from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.investigation.settlement.store import CandidateSettlementStore


async def run(*, once: bool) -> bool | None:
    database = Database(DatabaseSettings().database_url)
    store = CandidateSettlementStore(database.session_factory)
    try:
        if once:
            return await store.settle_next()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            loop.add_signal_handler(signal.SIGTERM, stop.set)
            loop.add_signal_handler(signal.SIGINT, stop.set)
        while not stop.is_set():
            if not await store.settle_next():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=2)
        return None
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="候选恢复结论的独立父运行收尾")
    parser.add_argument("--once", action="store_true", help="至多原子收尾一个父运行")
    args = parser.parse_args()
    with suppress(KeyboardInterrupt):
        asyncio.run(
            run(once=args.once),
            loop_factory=create_selector_event_loop if sys.platform == "win32" else None,
        )


if __name__ == "__main__":
    main()
