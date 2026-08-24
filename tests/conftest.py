import asyncio
import sys
from collections.abc import Callable, Mapping

import pytest

from opsswarm.database.asyncio_compat import create_selector_event_loop


def pytest_asyncio_loop_factories(
    config: pytest.Config,
    item: pytest.Item,
) -> Mapping[str, Callable[[], asyncio.AbstractEventLoop]]:
    """让 Windows 集成测试使用 Psycopg 支持的 Selector 事件循环。"""

    del config, item
    if sys.platform == "win32":
        return {"windows-selector": create_selector_event_loop}
    return {"default": asyncio.new_event_loop}
