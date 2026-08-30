"""不调用模型的变更调查工具冒烟命令。"""

import argparse
import json
from collections.abc import Sequence

from langchain_core.tools import BaseTool

from opsswarm.investigation.changes import ChangeApiOnlyInvestigationDataSource
from opsswarm.investigation.tools import EvidenceCollector, create_investigation_tools


def _parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查询本地变更服务并输出规范化变更证据")
    parser.add_argument("--lookback-hours", type=int, default=6, choices=range(1, 25))
    return parser.parse_args(argv)


def _changes_tool(tools: tuple[BaseTool, ...]) -> BaseTool:
    return next(tool for tool in tools if tool.name == "list_recent_changes")


def main(argv: Sequence[str] | None = None) -> None:
    """通过 Agent 实际使用的 LangChain 工具执行一次只读查询。"""

    args = _parse_args(argv)
    collector = EvidenceCollector()
    tools = create_investigation_tools(ChangeApiOnlyInvestigationDataSource(), collector)
    result = _changes_tool(tools).invoke(
        {
            "service_name": "order-service",
            "lookback_hours": args.lookback_hours,
        }
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
