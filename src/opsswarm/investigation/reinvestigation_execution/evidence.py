"""按实际窗口调用固定只读来源，阻塞变更查询保留底层容量约束。"""

import asyncio
from collections.abc import Sequence
from typing import Protocol
from uuid import UUID

from opsswarm.investigation.changes import ChangeApiOnlyInvestigationDataSource
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.investigation.query_execution import ReadOnlyQueryExecutor
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_execution.contracts import EffectiveWindow
from opsswarm.investigation.reinvestigation_execution.window import effective_window
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot
from opsswarm.observation_window import ObservationWindow
from opsswarm.telemetry_mcp.client import TelemetryMcpLogDataSource
from opsswarm.telemetry_metrics_mcp.client import TelemetryMcpMetricDataSource


class AsyncLogSource(Protocol):
    async def query_logs_in_window_async(
        self, *, service_name: str, window: ObservationWindow
    ) -> Sequence[EvidenceObservation]: ...


class AsyncMetricSource(Protocol):
    async def query_metrics_in_window_async(
        self, *, service_name: str, window: ObservationWindow
    ) -> Sequence[EvidenceObservation]: ...


class WindowedChangeSource(Protocol):
    def list_changes_in_window(
        self, *, service_name: str, window: ObservationWindow
    ) -> Sequence[EvidenceObservation]: ...


class ExecutionEvidenceSource:
    def __init__(
        self,
        *,
        logs: AsyncLogSource | None = None,
        metrics: AsyncMetricSource | None = None,
        changes: WindowedChangeSource | None = None,
        executor: ReadOnlyQueryExecutor | None = None,
    ) -> None:
        self._logs = logs if logs is not None else TelemetryMcpLogDataSource()
        self._metrics = metrics if metrics is not None else TelemetryMcpMetricDataSource()
        self._changes = changes if changes is not None else ChangeApiOnlyInvestigationDataSource()
        self._executor = executor if executor is not None else ReadOnlyQueryExecutor()
        self._slots = asyncio.Semaphore(3)
        self._closed = False

    async def collect(
        self, assignment: TaskAssignment, task_id: UUID, window: EffectiveWindow
    ) -> TaskEvidenceSnapshot:
        if self._closed:
            raise ReflowError("execution_source_closed", "只读取证来源已经关闭")
        if window != effective_window(assignment):
            raise ReflowError("execution_evidence_invalid", "实际窗口不是原任务范围的确定性内收")
        async with self._slots, asyncio.timeout(30):
            if self._closed:
                raise ReflowError("execution_source_closed", "只读取证来源已经关闭")
            if assignment.role is EvidenceKind.LOG:
                observations = await self._logs.query_logs_in_window_async(
                    service_name=assignment.service_name, window=window.window
                )
            elif assignment.role is EvidenceKind.METRIC:
                observations = await self._metrics.query_metrics_in_window_async(
                    service_name=assignment.service_name, window=window.window
                )
            else:
                observations = await self._executor.execute(
                    lambda: self._changes.list_changes_in_window(
                        service_name=assignment.service_name, window=window.window
                    )
                )
        if len(observations) > 20:
            raise ReflowError("execution_evidence_invalid", "专业来源返回过多证据")
        # 快照校验同时限制数量、字节数及每条证据的角色、来源、服务和精确窗口。
        return TaskEvidenceSnapshot(
            version="task-evidence-live-mcp-v1",
            data_source_mode="live",
            evidence_snapshot_id=f"reflow-v1:{task_id}",
            role=assignment.role,
            service_name=assignment.service_name,
            window_start=window.window.window_start,
            window_end=window.window.window_end,
            evidence=tuple(observations),
        )

    def close(self) -> None:
        self._closed = True
        self._executor.close()
