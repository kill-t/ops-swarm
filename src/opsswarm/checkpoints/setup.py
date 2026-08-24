"""初始化 LangGraph checkpoint 表的命令入口。"""

import asyncio
import sys

from opsswarm.checkpoints import (
    CheckpointSettings,
    ensure_separate_checkpoint_database,
    initialize_checkpoint_schema,
)
from opsswarm.database import DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop


def main() -> None:
    """从环境读取专用数据库 URL 并执行一次幂等初始化。"""

    settings = CheckpointSettings()
    ensure_separate_checkpoint_database(
        settings.checkpoint_database_url,
        DatabaseSettings().database_url,
    )
    loop_factory = create_selector_event_loop if sys.platform == "win32" else None
    asyncio.run(
        initialize_checkpoint_schema(settings.checkpoint_database_url),
        loop_factory=loop_factory,
    )
    print("LangGraph checkpoint 数据库初始化完成")


if __name__ == "__main__":
    main()
