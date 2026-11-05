"""组合受限适配器的真实现场调查数据源。"""

from collections.abc import Sequence
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any, Self, cast

from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.investigation.changes import ChangeApiOnlyInvestigationDataSource
from opsswarm.investigation.github_changes import (
    GitHubChangeOnlyInvestigationDataSource,
    GitHubChangeSettings,
    GitHubEnrichedChangeDataSource,
)
from opsswarm.investigation.live_window import (
    UnsupportedLiveTaskSource,
    WindowBoundLiveDataSource,
    WindowedLogDataSource,
    WindowedMetricDataSource,
)
from opsswarm.investigation.loki import LokiOnlyInvestigationDataSource
from opsswarm.investigation.models import EvidenceObservation
from opsswarm.investigation.prometheus import PrometheusOnlyInvestigationDataSource
from opsswarm.investigation.tasks.evidence import EvidencePolicyVersion
from opsswarm.investigation.tools import InvestigationDataSource

if TYPE_CHECKING:
    from uuid import UUID

    from opsswarm.investigation.dynamic_live_source import DynamicLiveDataSource
    from opsswarm.investigation.query_execution import ReadOnlyQueryExecutor
    from opsswarm.telemetry_mcp.client import TelemetryMcpClientSettings
    from opsswarm.telemetry_metrics_mcp.client import TelemetryMetricsMcpClientSettings


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
        mcp_logs: WindowedLogDataSource | None = None,
        mcp_metrics: WindowedMetricDataSource | None = None,
        metrics: InvestigationDataSource | None = None,
        changes: InvestigationDataSource | None = None,
    ) -> None:
        self._logs = logs if logs is not None else LokiOnlyInvestigationDataSource()
        self._mcp_logs = mcp_logs
        self._mcp_metrics = mcp_metrics
        self._metrics = metrics if metrics is not None else PrometheusOnlyInvestigationDataSource()
        self._changes = changes if changes is not None else ChangeApiOnlyInvestigationDataSource()

    def bind_window(
        self,
        *,
        service_name: str,
        cutoff: datetime,
        evidence_policy_version: EvidencePolicyVersion = "task-evidence-live-v1",
    ) -> WindowBoundLiveDataSource:
        if not (
            isinstance(self._logs, LokiOnlyInvestigationDataSource)
            and isinstance(self._metrics, PrometheusOnlyInvestigationDataSource)
            and isinstance(self._changes, ChangeApiOnlyInvestigationDataSource)
        ):
            raise UnsupportedLiveTaskSource("现场 v2 仅支持 Loki、Prometheus 和本地历史变更源")
        if evidence_policy_version == "task-evidence-live-v1":
            logs: WindowedLogDataSource = self._logs
            metrics: WindowedMetricDataSource = self._metrics
        elif evidence_policy_version == "task-evidence-live-mcp-log-v1":
            if self._mcp_logs is None:
                raise UnsupportedLiveTaskSource("现场 v2 的日志 MCP 来源未配置")
            logs = self._mcp_logs
            metrics = self._metrics
        elif evidence_policy_version == "task-evidence-live-mcp-v1":
            if self._mcp_logs is None or self._mcp_metrics is None:
                raise UnsupportedLiveTaskSource("现场 v2 的遥测 MCP 来源未完整配置")
            logs = self._mcp_logs
            metrics = self._mcp_metrics
        else:
            raise UnsupportedLiveTaskSource("现场 v2 的取证策略不受支持")
        return WindowBoundLiveDataSource(
            logs=logs,
            metrics=metrics,
            changes=self._changes,
            service_name=service_name,
            cutoff=cutoff,
        )

    def bind_dynamic_window(
        self,
        *,
        run_id: "UUID",
        service_name: str,
        cutoff: datetime,
        executor: "ReadOnlyQueryExecutor",
    ) -> "DynamicLiveDataSource":
        from opsswarm.investigation.dynamic_live_source import DynamicLiveDataSource
        from opsswarm.investigation.tasks.evidence import live_snapshot_id

        if (
            self._mcp_logs is None
            or self._mcp_metrics is None
            or not isinstance(self._changes, ChangeApiOnlyInvestigationDataSource)
        ):
            raise UnsupportedLiveTaskSource("现场动态调查需要完整MCP来源及本地历史变更源")
        return DynamicLiveDataSource(
            logs=self._mcp_logs,
            metrics=self._mcp_metrics,
            changes=self._changes,
            service_name=service_name,
            cutoff=cutoff,
            snapshot_id=live_snapshot_id(run_id, cutoff),
            executor=executor,
        )

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
    telemetry_mcp_settings: "TelemetryMcpClientSettings | None" = None,
    telemetry_metrics_mcp_settings: "TelemetryMetricsMcpClientSettings | None" = None,
) -> LiveInvestigationDataSource:
    """按显式配置组装现场来源，GitHub 模式缺少仓库配置时直接失败。"""

    from opsswarm.telemetry_mcp.client import (
        TelemetryMcpClientSettings,
        TelemetryMcpLogDataSource,
    )
    from opsswarm.telemetry_metrics_mcp.client import (
        TelemetryMcpMetricDataSource,
        TelemetryMetricsMcpClientSettings,
    )

    simulator = ChangeApiOnlyInvestigationDataSource()
    changes: InvestigationDataSource = simulator
    if settings.change_provider is LiveChangeProvider.SIMULATOR_AND_GITHUB:
        if github_settings is None:
            raise ValueError("启用 GitHub 变更补充时必须提供 GitHubChangeSettings")
        changes = GitHubEnrichedChangeDataSource(
            simulator,
            GitHubChangeOnlyInvestigationDataSource(github_settings),
        )
    return LiveInvestigationDataSource(
        changes=changes,
        mcp_logs=TelemetryMcpLogDataSource(telemetry_mcp_settings or TelemetryMcpClientSettings()),
        mcp_metrics=TelemetryMcpMetricDataSource(
            telemetry_metrics_mcp_settings or TelemetryMetricsMcpClientSettings()
        ),
    )
