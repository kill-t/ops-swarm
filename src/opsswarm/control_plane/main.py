"""使用兼容事件循环启动 OpsSwarm 控制面。"""

import argparse
import asyncio
import sys
from collections.abc import Sequence
from contextlib import suppress

import uvicorn

from opsswarm.control_plane.app import create_app
from opsswarm.database.asyncio_compat import create_selector_event_loop

app = create_app()


def parse_server_arguments(arguments: Sequence[str] | None = None) -> tuple[str, int]:
    """解析仅允许本机访问的 MVP 控制面启动参数。"""

    parser = argparse.ArgumentParser(description="启动 OpsSwarm 事故控制面")
    parser.add_argument(
        "--host",
        choices=("127.0.0.1", "localhost"),
        default="127.0.0.1",
        help="监听地址；当前阶段只允许本机访问",
    )
    parser.add_argument(
        "--port",
        type=_valid_port,
        default=8000,
        help="监听端口，默认 8000",
    )
    namespace = parser.parse_args(arguments)
    return str(namespace.host), int(namespace.port)


def _valid_port(raw_value: str) -> int:
    """拒绝超出 TCP 端口范围的启动参数。"""

    port = int(raw_value)
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("端口必须介于 1 和 65535 之间")
    return port


async def serve(host: str, port: int) -> None:
    """在调用方选定的事件循环中运行 Uvicorn。"""

    config = uvicorn.Config(app=app, host=host, port=port)
    await uvicorn.Server(config).serve()


def main(arguments: Sequence[str] | None = None) -> None:
    """Windows 显式使用 Psycopg 异步模式支持的 Selector 事件循环。"""

    host, port = parse_server_arguments(arguments)
    loop_factory = create_selector_event_loop if sys.platform == "win32" else None
    # Uvicorn 已完成生命周期关闭，不把正常的 Ctrl+C 显示成故障堆栈。
    with suppress(KeyboardInterrupt):
        asyncio.run(serve(host, port), loop_factory=loop_factory)


if __name__ == "__main__":
    main()
