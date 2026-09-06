"""固定只读取证端口的异步适配，复用既有来源校验。"""

from collections.abc import Callable, Sequence
from typing import Protocol

from opsswarm.investigation.models import EvidenceObservation
from opsswarm.investigation.query_execution import ReadOnlyQueryExecutor
from opsswarm.investigation.scenarios import FrozenScenarioDataSource
from opsswarm.investigation.tools import InvestigationDataSource


class AsyncInvestigationDataSource(Protocol):
    async def query_logs(
        self, *, service_name: str, lookback_minutes: int
    ) -> Sequence[EvidenceObservation]:
        """读取当前来源的有界日志观察。"""

    async def query_metrics(
        self, *, service_name: str, lookback_minutes: int
    ) -> Sequence[EvidenceObservation]:
        """读取当前来源的有界指标观察。"""

    async def list_recent_changes(
        self, *, service_name: str, lookback_hours: int
    ) -> Sequence[EvidenceObservation]:
        """读取当前来源的有界变更观察。"""


class ThreadedInvestigationDataSource:
    def __init__(
        self, source: InvestigationDataSource, executor: ReadOnlyQueryExecutor | None = None
    ) -> None:
        if executor is None and not isinstance(source, FrozenScenarioDataSource):
            raise ValueError("非冻结数据源必须显式注入受限取证执行器")
        self._source = source
        self._executor = executor

    async def _query(
        self, query: Callable[[], Sequence[EvidenceObservation]]
    ) -> Sequence[EvidenceObservation]:
        # 只有确定无网络的冻结来源可以内联；材料在等待成功后才交给调用方。
        return tuple(await self._executor.execute(query) if self._executor else query())

    async def query_logs(
        self, *, service_name: str, lookback_minutes: int
    ) -> Sequence[EvidenceObservation]:
        return await self._query(
            lambda: self._source.query_logs(
                service_name=service_name, lookback_minutes=lookback_minutes
            )
        )

    async def query_metrics(
        self, *, service_name: str, lookback_minutes: int
    ) -> Sequence[EvidenceObservation]:
        return await self._query(
            lambda: self._source.query_metrics(
                service_name=service_name, lookback_minutes=lookback_minutes
            )
        )

    async def list_recent_changes(
        self, *, service_name: str, lookback_hours: int
    ) -> Sequence[EvidenceObservation]:
        return await self._query(
            lambda: self._source.list_recent_changes(
                service_name=service_name, lookback_hours=lookback_hours
            )
        )
