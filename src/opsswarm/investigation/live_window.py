"""把已支持的现场适配器绑定到一个运行的固定服务和观测窗口。"""

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Protocol

from opsswarm.investigation.changes import ChangeApiOnlyInvestigationDataSource
from opsswarm.investigation.models import EvidenceObservation
from opsswarm.investigation.prometheus import PrometheusOnlyInvestigationDataSource
from opsswarm.observation_window import ObservationWindow


class UnsupportedLiveTaskSource(ValueError):
    """运行配置不支持固定现场取证，重试不能改变来源策略。"""


class WindowedLogDataSource(Protocol):
    """日志来源只能读取调用方给定的完整窗口。"""

    def query_logs_in_window(
        self, *, service_name: str, window: ObservationWindow
    ) -> Sequence[EvidenceObservation]: ...


class WindowBoundLiveDataSource:
    """只绑定查询参数；网络与领域校验仍由原适配器负责。"""

    def __init__(
        self,
        *,
        logs: WindowedLogDataSource,
        metrics: PrometheusOnlyInvestigationDataSource,
        changes: ChangeApiOnlyInvestigationDataSource,
        service_name: str,
        cutoff: datetime,
    ) -> None:
        self._logs, self._metrics, self._changes = logs, metrics, changes
        self._service_name = service_name
        self.telemetry_window = ObservationWindow(
            window_start=cutoff - timedelta(minutes=15), window_end=cutoff
        )
        self.change_window = ObservationWindow(
            window_start=cutoff - timedelta(hours=6), window_end=cutoff
        )

    def _require_scope(self, service_name: str, duration: int, expected: int) -> None:
        if service_name != self._service_name or duration != expected:
            raise ValueError("查询不能改变运行绑定的服务或观测窗口")

    def query_logs(
        self, *, service_name: str, lookback_minutes: int
    ) -> Sequence[EvidenceObservation]:
        self._require_scope(service_name, lookback_minutes, 15)
        return self._logs.query_logs_in_window(
            service_name=service_name, window=self.telemetry_window
        )

    def query_metrics(
        self, *, service_name: str, lookback_minutes: int
    ) -> Sequence[EvidenceObservation]:
        self._require_scope(service_name, lookback_minutes, 15)
        return self._metrics.query_metrics_in_window(
            service_name=service_name, window=self.telemetry_window
        )

    def list_recent_changes(
        self, *, service_name: str, lookback_hours: int
    ) -> Sequence[EvidenceObservation]:
        self._require_scope(service_name, lookback_hours, 6)
        return self._changes.list_changes_in_window(
            service_name=service_name, window=self.change_window
        )
