"""运行三故障调查评测，并可显式持久化完整报告。"""

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from opsswarm.database import Database, DatabaseSettings
from opsswarm.database.asyncio_compat import create_selector_event_loop
from opsswarm.evaluation import MVP_INVESTIGATION_DATASET_V1
from opsswarm.evaluation.fixtures import run_fixture_benchmark
from opsswarm.evaluation.persistence import EvaluationRunSubmission, EvaluationStore


@dataclass(frozen=True, slots=True)
class EvaluationCommandOptions:
    """评测命令中会改变持久化行为的显式参数。"""

    persist: bool
    idempotency_key: str | None
    runner_name: str


def parse_arguments(arguments: Sequence[str] | None = None) -> EvaluationCommandOptions:
    """解析离线评测参数；没有显式开关时绝不写数据库。"""

    parser = argparse.ArgumentParser(description="运行 OpsSwarm 三故障调查评测")
    parser.add_argument(
        "--persist",
        action="store_true",
        help="把评测集快照和报告写入业务 PostgreSQL",
    )
    parser.add_argument(
        "--idempotency-key",
        help="本次运行的稳定幂等键；使用 --persist 时必填",
    )
    parser.add_argument(
        "--runner-name",
        default="fixture-contract-v1",
        help="记录产生报告的运行器版本",
    )
    namespace = parser.parse_args(arguments)
    persist = bool(namespace.persist)
    idempotency_key = (
        str(namespace.idempotency_key) if namespace.idempotency_key is not None else None
    )
    if persist and idempotency_key is None:
        parser.error("使用 --persist 时必须提供 --idempotency-key")
    if not persist and idempotency_key is not None:
        parser.error("--idempotency-key 只能与 --persist 一起使用")
    return EvaluationCommandOptions(
        persist=persist,
        idempotency_key=idempotency_key,
        runner_name=str(namespace.runner_name),
    )


async def async_main(options: EvaluationCommandOptions) -> int:
    """运行离线契约基准，并按显式选项决定是否持久化。"""

    report = await run_fixture_benchmark()
    if not options.persist:
        print(report.model_dump_json(indent=2))
        return 0

    if options.idempotency_key is None:
        raise RuntimeError("持久化模式缺少幂等键")
    database = Database(DatabaseSettings().database_url)
    try:
        creation = await EvaluationStore(database.session_factory).save_report(
            EvaluationRunSubmission(
                dataset=MVP_INVESTIGATION_DATASET_V1,
                report=report,
                idempotency_key=options.idempotency_key,
                runner_name=options.runner_name,
                created_at=datetime.now(UTC),
            )
        )
    finally:
        await database.dispose()
    print(
        json.dumps(
            {
                "created": creation.created,
                "run": creation.run.model_dump(mode="json"),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


def main(arguments: Sequence[str] | None = None) -> int:
    """同步命令入口。"""

    options = parse_arguments(arguments)
    loop_factory = create_selector_event_loop if sys.platform == "win32" else None
    return asyncio.run(async_main(options), loop_factory=loop_factory)


if __name__ == "__main__":
    raise SystemExit(main())
