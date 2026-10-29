"""显式运行纯事务回流路由消费者，不装配模型或遥测客户端。"""

import argparse
import asyncio
import signal
import sys
from contextlib import suppress

from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.investigation.reinvestigation_routing.store import ReflowRoutingStore


async def run(*, once: bool) -> bool | None:
    database = Database(DatabaseSettings().database_url)
    store = ReflowRoutingStore(database.session_factory)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    if not once and sys.platform != "win32":
        loop.add_signal_handler(signal.SIGTERM, stop.set)
        loop.add_signal_handler(signal.SIGINT, stop.set)
    try:
        if once:
            return await store.record_next()
        while not stop.is_set():
            if not await store.record_next():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=2)
        return None
    finally:
        await database.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="回流审查确定性路由（不调用模型）")
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--once", action="store_true", help="至多消费一份审查结果")
    modes.add_argument("--continuous", action="store_true", help="持续发现并记录路由")
    args = parser.parse_args()
    with suppress(KeyboardInterrupt):
        result = asyncio.run(
            run(once=args.once),
            loop_factory=create_selector_event_loop if sys.platform == "win32" else None,
        )
        if args.once:
            print("已记录一份回流路由" if result else "当前没有可消费审查")


if __name__ == "__main__":
    main()
