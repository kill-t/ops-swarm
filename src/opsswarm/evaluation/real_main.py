"""显式运行一次受预算约束的真实模型单场景评测。"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.evaluation import MVP_INVESTIGATION_DATASET_V1
from opsswarm.evaluation.attempts import EvaluationAttemptStatus
from opsswarm.evaluation.persistence import EvaluationStore
from opsswarm.evaluation.real_model import (
    RealModelEvaluationSettings,
    RealModelSmokeRunner,
)
from opsswarm.investigation.settings import LlmSettings


@dataclass(frozen=True, slots=True)
class RealModelCommandOptions:
    """真实模型冒烟命令的显式实验身份。"""

    experiment_key: str
    repetition_index: int
    scenario_id: str
    idempotency_prefix: str


def parse_arguments(arguments: Sequence[str] | None = None) -> RealModelCommandOptions:
    """解析真实模型命令；没有完整身份时拒绝运行。"""

    parser = argparse.ArgumentParser(description="运行 OpsSwarm 真实模型单场景冒烟评测")
    parser.add_argument("--experiment-key", required=True, help="可比较重复运行共享的实验标识")
    parser.add_argument(
        "--repetition-index",
        type=int,
        default=1,
        help="本次运行在实验组内从 1 开始的重复序号",
    )
    parser.add_argument(
        "--scenario-id",
        default=MVP_INVESTIGATION_DATASET_V1.scenarios[0].scenario_id,
        choices=[item.scenario_id for item in MVP_INVESTIGATION_DATASET_V1.scenarios],
        help="本次只运行一个冻结场景",
    )
    parser.add_argument(
        "--idempotency-prefix",
        required=True,
        help="两种 Agent 尝试共享的幂等键前缀",
    )
    namespace = parser.parse_args(arguments)
    if not 1 <= namespace.repetition_index <= 1000:
        parser.error("--repetition-index 必须在 1 到 1000 之间")
    return RealModelCommandOptions(
        experiment_key=str(namespace.experiment_key),
        repetition_index=int(namespace.repetition_index),
        scenario_id=str(namespace.scenario_id),
        idempotency_prefix=str(namespace.idempotency_prefix),
    )


async def async_main(options: RealModelCommandOptions) -> int:
    """从本地环境加载密钥和价格，持久运行两种 Agent 模式。"""

    # 只有显式真实评测命令读取本地文件，测试与普通模块初始化不会碰触 .env。
    llm_settings = LlmSettings.from_dotenv()
    evaluation_settings = RealModelEvaluationSettings.from_dotenv()
    database = Database(DatabaseSettings().database_url)
    try:
        attempts = await RealModelSmokeRunner(
            store=EvaluationStore(database.session_factory),
            llm_settings=llm_settings,
            budget=evaluation_settings.to_budget(),
        ).run(
            MVP_INVESTIGATION_DATASET_V1,
            scenario_id=options.scenario_id,
            experiment_key=options.experiment_key,
            repetition_index=options.repetition_index,
            idempotency_prefix=options.idempotency_prefix,
        )
    finally:
        await database.dispose()
    print(
        json.dumps(
            {
                "attempts": [item.model_dump(mode="json") for item in attempts],
                "all_succeeded": all(
                    item.status is EvaluationAttemptStatus.SUCCEEDED for item in attempts
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return int(not all(item.status is EvaluationAttemptStatus.SUCCEEDED for item in attempts))


def main(arguments: Sequence[str] | None = None) -> int:
    """同步命令入口。"""

    options = parse_arguments(arguments)
    loop_factory = create_selector_event_loop if sys.platform == "win32" else None
    return asyncio.run(async_main(options), loop_factory=loop_factory)


if __name__ == "__main__":
    raise SystemExit(main())
