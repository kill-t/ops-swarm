"""checkpoint 与 Worker 集成测试共享的确定性调查桩。"""

import asyncio
from collections.abc import Sequence

from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationConclusion,
    InvestigationRequest,
    SpecialistReport,
)
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO, FrozenScenarioDataSource


class CountingDataSource(FrozenScenarioDataSource):
    """记录恢复进程实际重新查询了哪些只读数据源。"""

    def __init__(self) -> None:
        super().__init__(RELEASE_500_SCENARIO)
        self.calls = {kind: 0 for kind in EvidenceKind}

    def query_logs(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        self.calls[EvidenceKind.LOG] += 1
        return super().query_logs(
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )

    def query_metrics(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        self.calls[EvidenceKind.METRIC] += 1
        return super().query_metrics(
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )

    def list_recent_changes(
        self,
        *,
        service_name: str,
        lookback_hours: int,
    ) -> Sequence[EvidenceObservation]:
        self.calls[EvidenceKind.CHANGE] += 1
        return super().list_recent_changes(
            service_name=service_name,
            lookback_hours=lookback_hours,
        )


class SpecialistAnalystStub:
    """可选择让日志节点首次失败的专业分析桩。"""

    def __init__(self, *, fail_logs: bool = False) -> None:
        self._fail_logs = fail_logs
        self._completed_non_log_kinds: set[EvidenceKind] = set()
        self._non_log_reports_ready = asyncio.Event()

    async def analyze(
        self,
        *,
        specialist: EvidenceKind,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
    ) -> SpecialistReport:
        del request
        if specialist is EvidenceKind.LOG and self._fail_logs:
            await self._non_log_reports_ready.wait()
            # 给 LangGraph 留出保存两个成功节点 pending writes 的时间，避免测试依赖调度速度。
            await asyncio.sleep(0.1)
            raise RuntimeError("模拟日志节点进程中断")
        report = SpecialistReport(
            specialist=specialist,
            summary=f"{specialist.value} 节点完成",
            findings=("完成受限证据分析",),
            evidence_ids=tuple(item.evidence_id for item in evidence),
            confidence=0.9,
        )
        if specialist is not EvidenceKind.LOG:
            self._completed_non_log_kinds.add(specialist)
            if self._completed_non_log_kinds == {EvidenceKind.METRIC, EvidenceKind.CHANGE}:
                self._non_log_reports_ready.set()
        return report


class ConclusionSynthesizerStub:
    """使用真实收集证据生成固定的安全结论。"""

    async def synthesize(
        self,
        *,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
        reports: Sequence[SpecialistReport],
    ) -> InvestigationConclusion:
        del request, reports
        return InvestigationConclusion(
            root_cause="release_500_regression",
            confidence=0.96,
            summary="恢复后完成三类证据综合。",
            evidence_ids=tuple(item.evidence_id for item in evidence),
            recommendation="rollback_release",
            requires_human_approval=True,
            safety_notes=("回滚仍需人工审批。",),
        )
