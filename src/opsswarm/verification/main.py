"""持久恢复验证调度 Worker 命令入口。"""

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.verification import (
    FaultLabVerificationAdapter,
    VerificationSettings,
    VerificationWorkerSettings,
)
from opsswarm.verification.persistence import VerificationStore
from opsswarm.verification.service import VerificationService
from opsswarm.verification.worker import VerificationWorker

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_arguments(arguments: Sequence[str] | None = None) -> bool:
    """解析一次处理模式；默认作为常驻轮询进程运行。"""

    parser = argparse.ArgumentParser(description="启动 OpsSwarm 恢复验证调度 Worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="至多处理一个到期任务后退出，便于本地诊断",
    )
    return bool(parser.parse_args(arguments).once)


async def run(*, once: bool) -> bool | None:
    """创建独立数据库资源并运行一次或持续调度。"""

    database = Database(DatabaseSettings().database_url)
    worker_settings = VerificationWorkerSettings()
    store = VerificationStore(database.session_factory)
    service = VerificationService(
        store,
        FaultLabVerificationAdapter(VerificationSettings()),
        clock=utc_now,
    )
    worker = VerificationWorker(
        store=store,
        service=service,
        worker_id=f"{socket.gethostname()}-{os.getpid()}"[:100],
        clock=utc_now,
        lease_duration=timedelta(seconds=worker_settings.lease_duration_seconds),
        base_retry_delay=timedelta(seconds=worker_settings.base_retry_delay_seconds),
        max_retry_delay=timedelta(seconds=worker_settings.max_retry_delay_seconds),
    )
    logger.info(
        "启动恢复验证 Worker mode=%s poll_interval_seconds=%s lease_duration_seconds=%s",
        "once" if once else "forever",
        worker_settings.poll_interval_seconds,
        worker_settings.lease_duration_seconds,
    )
    try:
        if once:
            return await worker.run_once()
        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        if sys.platform != "win32":
            loop.add_signal_handler(signal.SIGTERM, stop_event.set)
            loop.add_signal_handler(signal.SIGINT, stop_event.set)
        await worker.run_forever(
            poll_interval=timedelta(seconds=worker_settings.poll_interval_seconds),
            stop_event=stop_event,
        )
        return None
    finally:
        await database.dispose()


def main(arguments: Sequence[str] | None = None) -> None:
    """Windows 显式使用 Psycopg 兼容事件循环。"""

    once = parse_arguments(arguments)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    loop_factory = create_selector_event_loop if sys.platform == "win32" else None
    processed = asyncio.run(run(once=once), loop_factory=loop_factory)
    if once:
        print("已处理一个恢复验证任务" if processed else "当前没有到期恢复验证任务")


if __name__ == "__main__":
    main()
