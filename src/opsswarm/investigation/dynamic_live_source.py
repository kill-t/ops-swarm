"""按持久专业任务的精确窗口执行受限现场查询。"""

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal

from opsswarm.investigation.changes import ChangeApiOnlyInvestigationDataSource
from opsswarm.investigation.live_window import WindowedLogDataSource, WindowedMetricDataSource
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.investigation.query_execution import ReadOnlyQueryExecutor
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot
from opsswarm.observation_window import ObservationWindow

POLICY_VERSION: Literal["task-evidence-live-dynamic-mcp-v1"] = "task-evidence-live-dynamic-mcp-v1"


def require_dynamic_live_assignment(
    assignment: TaskAssignment, *, service_name: str, cutoff: datetime, snapshot_id: str
) -> None:
    """模型只能收窄窗口，不能改变运行服务、截止、快照或专业上限。"""
    maximum = (
        timedelta(hours=6) if assignment.role is EvidenceKind.CHANGE else timedelta(minutes=15)
    )
    duration = assignment.window_end - assignment.window_start
    if (
        assignment.service_name != service_name
        or assignment.window_end != cutoff
        or assignment.evidence_snapshot_id != snapshot_id
        or not timedelta(0) < duration <= maximum
        or duration.total_seconds() % 60
        or assignment.window_start.microsecond
        or assignment.window_end.microsecond
    ):
        raise TaskFactConflict(
            "task_evidence_scope_mismatch", "现场动态任务必须使用运行范围内的完整分钟窗口"
        )


class DynamicLiveDataSource:
    """只读取日志/指标MCP及本地变更，不提供直连或冻结回退。"""

    def __init__(
        self,
        *,
        logs: WindowedLogDataSource,
        metrics: WindowedMetricDataSource,
        changes: ChangeApiOnlyInvestigationDataSource,
        service_name: str,
        cutoff: datetime,
        snapshot_id: str,
        executor: ReadOnlyQueryExecutor,
    ) -> None:
        if service_name != "order-service" or cutoff.tzinfo is None or cutoff.utcoffset() is None:
            raise ValueError("现场动态来源仅支持带UTC截止的订单服务演练")
        self.snapshot_time = cutoff.astimezone(UTC)
        # 创建时复核整秒及Unix范围，不能等到已经派发任务才发现非法截止。
        ObservationWindow(
            window_start=self.snapshot_time - timedelta(hours=6), window_end=self.snapshot_time
        )
        self.snapshot_id = snapshot_id
        self._service_name = service_name
        self._logs, self._metrics, self._changes = logs, metrics, changes
        self._executor = executor

    async def collect(self, assignment: TaskAssignment) -> tuple[EvidenceObservation, ...]:
        require_dynamic_live_assignment(
            assignment,
            service_name=self._service_name,
            cutoff=self.snapshot_time,
            snapshot_id=self.snapshot_id,
        )
        return await self._executor.execute(lambda: self._collect(assignment))

    def _collect(self, assignment: TaskAssignment) -> tuple[EvidenceObservation, ...]:
        window = ObservationWindow(
            window_start=assignment.window_start, window_end=assignment.window_end
        )
        evidence: Sequence[EvidenceObservation]
        if assignment.role is EvidenceKind.LOG:
            evidence = self._logs.query_logs_in_window(
                service_name=self._service_name, window=window
            )
        elif assignment.role is EvidenceKind.METRIC:
            evidence = self._metrics.query_metrics_in_window(
                service_name=self._service_name, window=window
            )
        else:
            evidence = self._changes.list_changes_in_window(
                service_name=self._service_name, window=window
            )
        return TaskEvidenceSnapshot(
            version=POLICY_VERSION,
            data_source_mode="live",
            evidence_snapshot_id=self.snapshot_id,
            role=assignment.role,
            service_name=self._service_name,
            window_start=window.window_start,
            window_end=window.window_end,
            evidence=tuple(evidence),
        ).evidence
