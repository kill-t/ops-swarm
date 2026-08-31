"""组合受限适配器的真实现场调查数据源。"""

from collections.abc import Sequence
from enum import StrEnum
from typing import Any, Self, cast

from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.investigation.changes import ChangeApiOnlyInvestigationDataSource
from opsswarm.investigation.github_changes import (
    GitHubChangeOnlyInvestigationDataSource,
    GitHubChangeSettings,
    GitHubEnrichedChangeDataSource,
)
from opsswarm.investigation.loki import LokiOnlyInvestigationDataSource
from opsswarm.investigation.models import EvidenceObservation
from opsswarm.investigation.prometheus import PrometheusOnlyInvestigationDataSource
from opsswarm.investigation.tools import InvestigationDataSource


class LiveChangeProvider(StrEnum):
    """现场调查允许使用的变更来源组合。"""

    SIMULATOR = "simulator"
    SIMULATOR_AND_GITHUB = "simulator_and_github"


class LiveInvestigationSettings(BaseSettings):
    """显式选择现场变更来源，默认保持确定性模拟器。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_INVESTIGATION_", extra="ignore")

    change_provider: LiveChangeProvider = LiveChangeProvider.SIMULATOR

    @classmethod
    def from_dotenv(cls) -> Self:
        """仅供显式本地命令读取项目根目录的 .env。"""

        constructor = cast(Any, cls)
        return cast(Self, constructor(_env_file=".env", _env_file_encoding="utf-8"))


class LiveInvestigationDataSource:
    """按证据类型委托固定适配器，任何来源失败都原样向上返回。"""

    def __init__(
        self,
        *,
        logs: InvestigationDataSource | None = None,
        metrics: InvestigationDataSource | None = None,
        changes: InvestigationDataSource | None = None,
    ) -> None:
        self._logs = logs if logs is not None else LokiOnlyInvestigationDataSource()
        self._metrics = metrics if metrics is not None else PrometheusOnlyInvestigationDataSource()
        self._changes = changes if changes is not None else ChangeApiOnlyInvestigationDataSource()

    def query_logs(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        return self._logs.query_logs(
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )

    def query_metrics(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        return self._metrics.query_metrics(
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )

    def list_recent_changes(
        self,
        *,
        service_name: str,
        lookback_hours: int,
    ) -> Sequence[EvidenceObservation]:
        return self._changes.list_recent_changes(
            service_name=service_name,
            lookback_hours=lookback_hours,
        )


def create_live_investigation_data_source(
    settings: LiveInvestigationSettings,
    *,
    github_settings: GitHubChangeSettings | None = None,
) -> LiveInvestigationDataSource:
    """按显式配置组装现场来源，GitHub 模式缺少仓库配置时直接失败。"""

    simulator = ChangeApiOnlyInvestigationDataSource()
    changes: InvestigationDataSource = simulator
    if settings.change_provider is LiveChangeProvider.SIMULATOR_AND_GITHUB:
        if github_settings is None:
            raise ValueError("启用 GitHub 变更补充时必须提供 GitHubChangeSettings")
        changes = GitHubEnrichedChangeDataSource(
            simulator,
            GitHubChangeOnlyInvestigationDataSource(github_settings),
        )
    return LiveInvestigationDataSource(changes=changes)
