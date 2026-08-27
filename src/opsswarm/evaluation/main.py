"""输出不访问网络的三故障调查评测报告。"""

import asyncio

from opsswarm.evaluation.fixtures import run_fixture_benchmark


async def async_main() -> int:
    """运行离线契约基准并把完整报告写到标准输出。"""

    report = await run_fixture_benchmark()
    print(report.model_dump_json(indent=2))
    return 0


def main() -> int:
    """同步命令入口。"""

    return asyncio.run(async_main())


if __name__ == "__main__":
    raise SystemExit(main())
