"""异步数据库在 Windows 上的事件循环兼容策略。"""

import asyncio
import selectors


def create_selector_event_loop() -> asyncio.AbstractEventLoop:
    """创建 Psycopg 异步模式支持的 Selector 事件循环。"""

    return asyncio.SelectorEventLoop(selectors.SelectSelector())
