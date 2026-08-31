"""独立调查 Worker 的常驻与单次诊断入口。"""

import argparse
import asyncio
import logging
import os
import signal
import socket
import sys
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from opsswarm.checkpoints import (
    CheckpointSettings,
    ensure_separate_checkpoint_database,
    open_checkpoint_saver,
)
from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.investigation.github_changes import GitHubChangeSettings
from opsswarm.investigation.live import (
    LiveChangeProvider,
    LiveInvestigationSettings,
    create_live_investigation_data_source,
)
from opsswarm.investigation.models import InvestigationDataSourceMode
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.parallel_model import (
    LangChainConclusionSynthesizer,
    LangChainSpecialistAnalyst,
)
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource
from opsswarm.investigation.settings import (
    LlmSettings,
    ModelPricingSettings,
    create_openai_compatible_model,
)
from opsswarm.investigation.tools import InvestigationDataSource
from opsswarm.observability import create_tracing_runtime
from opsswarm.worker.processor import (
    LangChainInvestigationProcessor,
    LangGraphMultiAgentProcessor,
    ModeRoutingProcessor,
)
from opsswarm.worker.runner import InvestigationWorker
from opsswarm.worker.settings import InvestigationWorkerSettings
from opsswarm.worker.usage import ModelUsageTracker

logger = logging.getLogger(__name__)


def utc_now() -> datetime:
    return datetime.now(UTC)


def parse_arguments(arguments: Sequence[str] | None = None) -> bool:
    """解析一次处理模式；默认作为常驻轮询进程运行。"""

    parser = argparse.ArgumentParser(description="启动 OpsSwarm 调查 Worker")
    parser.add_argument(
        "--once",
        action="store_true",
        help="至多处理一个匹配任务后退出，便于本地诊断",
    )
    return bool(parser.parse_args(arguments).once)


async def run(*, once: bool) -> bool | None:
    """使用显式模型配置处理一次或持续轮询，并释放所有资源。"""

    database_settings = DatabaseSettings()
    checkpoint_settings = CheckpointSettings()
    worker_settings = InvestigationWorkerSettings.from_dotenv()
    ensure_separate_checkpoint_database(
        checkpoint_settings.checkpoint_database_url,
        database_settings.database_url,
    )
    database = Database(database_settings.database_url)
    tracing_runtime = create_tracing_runtime(
        f"opsswarm-investigation-worker-{worker_settings.data_source_mode.value}"
    )
    tracer = tracing_runtime.get_tracer(__name__)
    try:
        llm_settings = LlmSettings.from_dotenv()
        model_pricing = ModelPricingSettings.from_dotenv().to_pricing()
        usage_tracker = ModelUsageTracker(model_pricing)
        model = create_openai_compatible_model(
            llm_settings,
            callbacks=(usage_tracker,),
        )
        data_source: InvestigationDataSource
        if worker_settings.data_source_mode is InvestigationDataSourceMode.LIVE:
            live_settings = LiveInvestigationSettings.from_dotenv()
            github_settings = (
                GitHubChangeSettings.from_dotenv()
                if live_settings.change_provider is LiveChangeProvider.SIMULATOR_AND_GITHUB
                else None
            )
            data_source = create_live_investigation_data_source(
                live_settings,
                github_settings=github_settings,
            )
            evaluation_scenario = None
        else:
            data_source = FrozenScenarioDataSource(RELEASE_500_SCENARIO)
            evaluation_scenario = RELEASE_500_SCENARIO
        async with open_checkpoint_saver(checkpoint_settings.checkpoint_database_url) as saver:
            multi_agent_investigator = LangGraphParallelInvestigator(
                data_source=data_source,
                analyst=LangChainSpecialistAnalyst(
                    model,
                    structured_output_method=llm_settings.structured_output_method,
                ),
                synthesizer=LangChainConclusionSynthesizer(
                    model,
                    structured_output_method=llm_settings.structured_output_method,
                ),
                checkpointer=saver,
                tracer=tracer,
            )
            processor = ModeRoutingProcessor(
                single_agent=LangChainInvestigationProcessor(
                    model=model,
                    model_name=llm_settings.model,
                    data_source=data_source,
                    data_source_mode=worker_settings.data_source_mode,
                    evaluation_scenario=evaluation_scenario,
                    tracer=tracer,
                ),
                multi_agent=LangGraphMultiAgentProcessor(
                    investigator=multi_agent_investigator,
                    model_name=llm_settings.model,
                    data_source_mode=worker_settings.data_source_mode,
                    evaluation_scenario=evaluation_scenario,
                    tracer=tracer,
                ),
            )
            worker = InvestigationWorker(
                store=InvestigationStore(database.session_factory),
                processor=processor,
                worker_id=f"{socket.gethostname()}-{os.getpid()}"[:100],
                clock=utc_now,
                lease_duration=timedelta(seconds=worker_settings.lease_duration_seconds),
                processing_timeout=timedelta(seconds=worker_settings.processing_timeout_seconds),
                base_retry_delay=timedelta(seconds=worker_settings.base_retry_delay_seconds),
                max_retry_delay=timedelta(seconds=worker_settings.max_retry_delay_seconds),
                data_source_mode=worker_settings.data_source_mode,
                model_name=llm_settings.model,
                usage_tracker=usage_tracker,
                tracer=tracer,
            )
            logger.info(
                "启动调查 Worker mode=%s data_source_mode=%s poll_interval_seconds=%s",
                "once" if once else "forever",
                worker_settings.data_source_mode.value,
                worker_settings.poll_interval_seconds,
            )
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
        tracing_runtime.shutdown()


def main(arguments: Sequence[str] | None = None) -> None:
    """默认常驻运行；Windows 显式使用 Psycopg 兼容事件循环。"""

    once = parse_arguments(arguments)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    loop_factory = create_selector_event_loop if sys.platform == "win32" else None
    try:
        processed = asyncio.run(run(once=once), loop_factory=loop_factory)
    except KeyboardInterrupt:
        return
    if once:
        print("已处理一个调查任务" if processed else "当前没有可处理的调查任务")


if __name__ == "__main__":
    main()
