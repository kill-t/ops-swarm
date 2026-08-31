"""不调用模型的统一现场调查数据源冒烟命令。"""

import json

from opsswarm.investigation.github_changes import GitHubChangeSettings
from opsswarm.investigation.live import (
    LiveChangeProvider,
    LiveInvestigationSettings,
    create_live_investigation_data_source,
)
from opsswarm.investigation.tools import EvidenceCollector, create_investigation_tools


def main() -> None:
    """通过 Agent 使用的三个 LangChain 工具读取同一现场。"""

    collector = EvidenceCollector()
    settings = LiveInvestigationSettings.from_dotenv()
    github_settings = (
        GitHubChangeSettings.from_dotenv()
        if settings.change_provider is LiveChangeProvider.SIMULATOR_AND_GITHUB
        else None
    )
    source = create_live_investigation_data_source(
        settings,
        github_settings=github_settings,
    )
    tools = create_investigation_tools(source, collector)
    results = {
        "logs": tools[0].invoke({"service_name": "order-service", "lookback_minutes": 15}),
        "metrics": tools[1].invoke({"service_name": "order-service", "lookback_minutes": 15}),
        "changes": tools[2].invoke({"service_name": "order-service", "lookback_hours": 6}),
        "tool_call_count": collector.tool_call_count,
    }
    print(json.dumps(results, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
