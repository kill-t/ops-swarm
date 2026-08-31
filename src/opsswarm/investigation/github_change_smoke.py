"""不调用模型的 GitHub 只读变更来源冒烟命令。"""

import argparse
import json
from collections.abc import Sequence

from opsswarm.change_records.models import SUPPORTED_SERVICE
from opsswarm.investigation.github_changes import (
    GitHubChangeOnlyInvestigationDataSource,
    GitHubChangeSettings,
)


def parse_arguments(arguments: Sequence[str] | None = None) -> int:
    """限制冒烟查询只能读取最近 1 到 24 小时。"""

    parser = argparse.ArgumentParser(description="读取固定 GitHub 仓库的只读变更证据")
    parser.add_argument(
        "--lookback-hours",
        type=int,
        choices=range(1, 25),
        default=6,
        metavar="1-24",
    )
    return int(parser.parse_args(arguments).lookback_hours)


def main(arguments: Sequence[str] | None = None) -> None:
    """从 .env 读取固定仓库与令牌，只输出规范化证据。"""

    lookback_hours = parse_arguments(arguments)
    source = GitHubChangeOnlyInvestigationDataSource(GitHubChangeSettings.from_dotenv())
    evidence = source.list_recent_changes(
        service_name=SUPPORTED_SERVICE,
        lookback_hours=lookback_hours,
    )
    print(
        json.dumps(
            [item.model_dump(mode="json") for item in evidence],
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
