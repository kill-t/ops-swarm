"""显式运行受批次预算约束的真实模型多场景评测。"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.evaluation import get_mvp_investigation_dataset
from opsswarm.evaluation.persistence import EvaluationStore
from opsswarm.evaluation.real_batch import RealModelBatchRunner
from opsswarm.evaluation.real_model import (
    RealModelEvaluationSettings,
    RealModelSmokeRunner,
)
from opsswarm.investigation.settings import LlmSettings


@dataclass(frozen=True, slots=True)
class RealModelBatchCommandOptions:
    """批量真实评测的显式范围和实验身份。"""

    experiment_key: str
    idempotency_prefix: str
    dataset_version: int
    repetition_start: int
    repetitions: int
    scenario_ids: tuple[str, ...]

    @property
    def repetition_indexes(self) -> tuple[int, ...]:
        return tuple(range(self.repetition_start, self.repetition_start + self.repetitions))


def parse_arguments(arguments: Sequence[str] | None = None) -> RealModelBatchCommandOptions:
    """解析小规模批量计划，默认覆盖全部冻结场景一次。"""

    parser = argparse.ArgumentParser(description="运行 OpsSwarm 真实模型多场景批量评测")
    parser.add_argument("--experiment-key", required=True, help="所有重复轮共享的实验标识")
    parser.add_argument("--idempotency-prefix", required=True, help="本批次幂等键前缀")
    parser.add_argument(
        "--dataset-version",
        type=int,
        choices=(1, 2),
        default=1,
        help="冻结评测集版本；默认 1 以保持旧命令语义",
    )
    parser.add_argument("--repetition-start", type=int, default=1, help="起始重复轮次")
    parser.add_argument("--repetitions", type=int, default=1, help="连续重复轮数，最多 10")
    parser.add_argument(
        "--scenario-id",
        action="append",
        dest="scenario_ids",
        help="可重复传入；不传时运行全部冻结场景",
    )
    namespace = parser.parse_args(arguments)
    if not 1 <= namespace.repetitions <= 10:
        parser.error("--repetitions 必须在 1 到 10 之间")
    repetition_end = namespace.repetition_start + namespace.repetitions - 1
    if namespace.repetition_start < 1 or repetition_end > 1000:
        parser.error("批量重复轮次必须在 1 到 1000 之间")
    dataset = get_mvp_investigation_dataset(int(namespace.dataset_version))
    scenario_choices = tuple(item.scenario_id for item in dataset.scenarios)
    scenario_ids = tuple(namespace.scenario_ids or scenario_choices)
    if len(set(scenario_ids)) != len(scenario_ids):
        parser.error("--scenario-id 不能重复")
    unknown_scenarios = set(scenario_ids) - set(scenario_choices)
    if unknown_scenarios:
        parser.error(f"场景不属于评测集 v{namespace.dataset_version}：{sorted(unknown_scenarios)}")
    return RealModelBatchCommandOptions(
        experiment_key=str(namespace.experiment_key),
        idempotency_prefix=str(namespace.idempotency_prefix),
        dataset_version=int(namespace.dataset_version),
        repetition_start=int(namespace.repetition_start),
        repetitions=int(namespace.repetitions),
        scenario_ids=scenario_ids,
    )


async def async_main(options: RealModelBatchCommandOptions) -> int:
    """加载本地密钥和预算，运行批量计划并输出派生报告。"""

    llm_settings = LlmSettings.from_dotenv()
    evaluation_settings = RealModelEvaluationSettings.from_dotenv()
    budget = evaluation_settings.to_budget()
    dataset = get_mvp_investigation_dataset(options.dataset_version)
    database = Database(DatabaseSettings().database_url)
    try:
        smoke_runner = RealModelSmokeRunner(
            store=EvaluationStore(database.session_factory),
            llm_settings=llm_settings,
            budget=budget,
        )
        report = await RealModelBatchRunner(
            smoke_runner=smoke_runner,
            budget=budget,
            max_batch_cost_usd=evaluation_settings.max_batch_cost_usd,
        ).run(
            dataset,
            experiment_key=options.experiment_key,
            repetition_indexes=options.repetition_indexes,
            scenario_ids=options.scenario_ids,
            idempotency_prefix=options.idempotency_prefix,
        )
    finally:
        await database.dispose()
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return int(report.single_agent.failed_count > 0 or report.multi_agent.failed_count > 0)


def main(arguments: Sequence[str] | None = None) -> int:
    """同步命令入口。"""

    options = parse_arguments(arguments)
    loop_factory = create_selector_event_loop if sys.platform == "win32" else None
    return asyncio.run(async_main(options), loop_factory=loop_factory)


if __name__ == "__main__":
    raise SystemExit(main())
