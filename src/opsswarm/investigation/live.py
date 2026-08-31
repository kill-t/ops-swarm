"""组合三个受限适配器的真实现场调查数据源。"""

from collections.abc import Sequence

from opsswarm.investigation.changes import ChangeApiOnlyInvestigationDataSource
from opsswarm.investigation.loki import LokiOnlyInvestigationDataSource
from opsswarm.investigation.models import EvidenceObservation
from opsswarm.investigation.prometheus import PrometheusOnlyInvestigationDataSource
from opsswarm.investigation.tools import InvestigationDataSource


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
