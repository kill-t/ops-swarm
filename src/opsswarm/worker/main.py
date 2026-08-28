"""独立 Worker 的单次处理命令入口。"""

import asyncio
import os
import socket
import sys
from datetime import UTC, datetime

from opsswarm.checkpoints import (
    CheckpointSettings,
    ensure_separate_checkpoint_database,
    open_checkpoint_saver,
)
from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.parallel_model import (
    LangChainConclusionSynthesizer,
    LangChainSpecialistAnalyst,
)
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource
from opsswarm.investigation.settings import LlmSettings, create_openai_compatible_model
from opsswarm.worker.processor import (
    LangChainBaselineProcessor,
    LangGraphMultiAgentProcessor,
    ModeRoutingProcessor,
)
from opsswarm.worker.runner import InvestigationWorker


def utc_now() -> datetime:
    return datetime.now(UTC)


async def run_once() -> bool:
    """使用显式模型配置处理一个持久任务，并确保释放数据库连接池。"""

    database_settings = DatabaseSettings()
    checkpoint_settings = CheckpointSettings()
    ensure_separate_checkpoint_database(
        checkpoint_settings.checkpoint_database_url,
        database_settings.database_url,
    )
    database = Database(database_settings.database_url)
    try:
        llm_settings = LlmSettings()
        model = create_openai_compatible_model(llm_settings)
        async with open_checkpoint_saver(checkpoint_settings.checkpoint_database_url) as saver:
            multi_agent_investigator = LangGraphParallelInvestigator(
                data_source=FrozenScenarioDataSource(RELEASE_500_SCENARIO),
                analyst=LangChainSpecialistAnalyst(
                    model,
                    structured_output_method=llm_settings.structured_output_method,
                ),
                synthesizer=LangChainConclusionSynthesizer(
                    model,
                    structured_output_method=llm_settings.structured_output_method,
                ),
                checkpointer=saver,
            )
            processor = ModeRoutingProcessor(
                single_agent=LangChainBaselineProcessor(
                    model=model,
                    model_name=llm_settings.model,
                ),
                multi_agent=LangGraphMultiAgentProcessor(
                    investigator=multi_agent_investigator,
                    model_name=llm_settings.model,
                ),
            )
            worker = InvestigationWorker(
                store=InvestigationStore(database.session_factory),
                processor=processor,
                worker_id=f"{socket.gethostname()}-{os.getpid()}"[:100],
                clock=utc_now,
            )
            return await worker.run_once()
    finally:
        await database.dispose()


def main() -> None:
    """运行一次后退出，后续守护循环由独立切片实现。"""

    loop_factory = create_selector_event_loop if sys.platform == "win32" else None
    processed = asyncio.run(run_once(), loop_factory=loop_factory)
    print("已处理一个调查任务" if processed else "当前没有可处理的调查任务")


if __name__ == "__main__":
    main()
