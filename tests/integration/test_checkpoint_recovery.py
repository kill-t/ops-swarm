"""使用独立真实 PostgreSQL 数据库验证 LangGraph 节点级恢复。"""

import asyncio
from collections.abc import Iterator, Sequence
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from sqlalchemy.engine import make_url

from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.investigation.graph_thread import build_graph_thread_id
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationConclusion,
    InvestigationRequest,
    SpecialistReport,
)
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.scenarios import (
    RELEASE_500_SCENARIO,
    FrozenScenarioDataSource,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(scope="session")
def checkpoint_database_url(migrated_database_url: str) -> Iterator[str]:
    """在同一临时 PostgreSQL 实例中创建隔离的 checkpoint 数据库。"""

    source_url = make_url(migrated_database_url)
    checkpoint_name = "opsswarm_checkpoints"
    admin_url = source_url.set(drivername="postgresql", database="postgres").render_as_string(
        hide_password=False
    )
    checkpoint_url = source_url.set(
        drivername="postgresql",
        database=checkpoint_name,
    ).render_as_string(hide_password=False)
    with psycopg.connect(admin_url, autocommit=True) as connection:
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(checkpoint_name)))
    yield checkpoint_url


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


async def test_postgres_checkpoint_resumes_only_failed_parallel_node(
    checkpoint_database_url: str,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    await initialize_checkpoint_schema(checkpoint_database_url)
    thread_id = build_graph_thread_id(uuid4())
    first_data_source = CountingDataSource()

    async with open_checkpoint_saver(checkpoint_database_url) as first_saver:
        first_graph = LangGraphParallelInvestigator(
            data_source=first_data_source,
            analyst=SpecialistAnalystStub(fail_logs=True),
            synthesizer=ConclusionSynthesizerStub(),
            checkpointer=first_saver,
        )
        with pytest.raises(RuntimeError, match="模拟日志节点进程中断"):
            await first_graph.investigate(
                RELEASE_500_SCENARIO.request,
                thread_id=thread_id,
            )

    assert first_data_source.calls == {
        EvidenceKind.LOG: 1,
        EvidenceKind.METRIC: 1,
        EvidenceKind.CHANGE: 1,
    }

    recovered_data_source = CountingDataSource()
    async with open_checkpoint_saver(checkpoint_database_url) as second_saver:
        recovered_graph = LangGraphParallelInvestigator(
            data_source=recovered_data_source,
            analyst=SpecialistAnalystStub(),
            synthesizer=ConclusionSynthesizerStub(),
            checkpointer=second_saver,
        )
        outcome = await recovered_graph.resume(thread_id=thread_id)
        latest_checkpoint = await second_saver.aget_tuple(
            {"configurable": {"thread_id": thread_id}}
        )

    assert recovered_data_source.calls == {
        EvidenceKind.LOG: 1,
        EvidenceKind.METRIC: 0,
        EvidenceKind.CHANGE: 0,
    }
    assert outcome.result.tool_call_count == 3
    assert set(outcome.result.returned_evidence_ids) == RELEASE_500_SCENARIO.required_evidence_ids
    assert latest_checkpoint is not None
    assert latest_checkpoint.checkpoint["channel_values"]["conclusion"] == outcome.result.conclusion
