"""不调用模型的 Prometheus 调查工具冒烟命令。"""

import argparse
import json
from collections.abc import Sequence

from langchain_core.tools import BaseTool

from opsswarm.investigation.prometheus import PrometheusOnlyInvestigationDataSource
from opsswarm.investigation.tools import EvidenceCollector, create_investigation_tools


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查询本地 Prometheus 并输出规范化指标证据")
    parser.add_argument("--lookback-minutes", type=int, default=15, choices=range(1, 61))
    return parser.parse_args(argv)


def _metrics_tool(tools: tuple[BaseTool, ...]) -> BaseTool:
    return next(tool for tool in tools if tool.name == "query_service_metrics")


def main(argv: Sequence[str] | None = None) -> None:
    """通过 Agent 实际使用的工具适配层执行一次只读查询。"""

    args = _parse_args(argv)
    collector = EvidenceCollector()
    tools = create_investigation_tools(PrometheusOnlyInvestigationDataSource(), collector)
    result = _metrics_tool(tools).invoke(
        {
            "service_name": "order-service",
            "lookback_minutes": args.lookback_minutes,
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
