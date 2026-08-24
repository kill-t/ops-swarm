"""只读调查数据源协议与 LangChain 工具适配。"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from threading import Lock
from typing import Protocol

from langchain_core.tools import BaseTool
from langchain_core.tools import tool as langchain_tool
from pydantic import BaseModel, ConfigDict, Field

from opsswarm.investigation.models import EvidenceObservation


class InvestigationDataSource(Protocol):
    """把真实观测系统与 Agent 框架隔离的只读端口。"""

    def query_logs(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        """查询指定服务在受限时间窗内的日志观察。"""

    def query_metrics(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        """查询指定服务在受限时间窗内的指标观察。"""

    def list_recent_changes(
        self,
        *,
        service_name: str,
        lookback_hours: int,
    ) -> Sequence[EvidenceObservation]:
        """查询指定服务在受限时间窗内的变更记录。"""


class ServiceMinuteQuery(BaseModel):
    """日志与指标工具的有界查询参数。"""

    model_config = ConfigDict(extra="forbid")

    service_name: str = Field(min_length=1, max_length=100)
    lookback_minutes: int = Field(default=15, ge=1, le=60)


class ServiceHourQuery(BaseModel):
    """变更工具的有界查询参数。"""

    model_config = ConfigDict(extra="forbid")

    service_name: str = Field(min_length=1, max_length=100)
    lookback_hours: int = Field(default=6, ge=1, le=24)


@dataclass(slots=True)
class EvidenceCollector:
    """记录本轮工具实际暴露给模型的证据，供结论交叉校验。"""

    returned_ids: set[str] = field(default_factory=set)
    tool_call_count: int = 0
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record(self, observations: Sequence[EvidenceObservation]) -> None:
        """并行工具调用时也原子更新本轮统计。"""

        with self._lock:
            self.tool_call_count += 1
            self.returned_ids.update(item.evidence_id for item in observations)


def _serialize_observations(
    observations: Sequence[EvidenceObservation],
) -> list[dict[str, object]]:
    return [dict(item.model_dump(mode="json")) for item in observations]


def create_investigation_tools(
    data_source: InvestigationDataSource,
    collector: EvidenceCollector,
) -> tuple[BaseTool, ...]:
    """为单次调查创建三个只读、有界且可审计的 LangChain 工具。"""

    @langchain_tool("query_service_logs", args_schema=ServiceMinuteQuery)
    def query_service_logs(
        service_name: str,
        lookback_minutes: int = 15,
    ) -> list[dict[str, object]]:
        """读取服务最近日志，只返回已经标准化的证据观察，不修改被调查系统。"""

        observations = data_source.query_logs(
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )
        collector.record(observations)
        return _serialize_observations(observations)

    @langchain_tool("query_service_metrics", args_schema=ServiceMinuteQuery)
    def query_service_metrics(
        service_name: str,
        lookback_minutes: int = 15,
    ) -> list[dict[str, object]]:
        """读取服务最近指标，只返回已经标准化的证据观察，不修改被调查系统。"""

        observations = data_source.query_metrics(
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )
        collector.record(observations)
        return _serialize_observations(observations)

    @langchain_tool("list_recent_changes", args_schema=ServiceHourQuery)
    def list_recent_changes(
        service_name: str,
        lookback_hours: int = 6,
    ) -> list[dict[str, object]]:
        """读取服务最近发布与配置变更，不创建、回滚或修改任何变更。"""

        observations = data_source.list_recent_changes(
            service_name=service_name,
            lookback_hours=lookback_hours,
        )
        collector.record(observations)
        return _serialize_observations(observations)

    return query_service_logs, query_service_metrics, list_recent_changes
