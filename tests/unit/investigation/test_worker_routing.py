import pytest

from opsswarm.investigation.challenge import challenge_investigation
from opsswarm.investigation.models import (
    EvidenceKind,
    InvestigationArtifacts,
    InvestigationConclusion,
    InvestigationDataSourceMode,
    InvestigationMode,
    InvestigationRequest,
    InvestigationRunResult,
    MultiAgentInvestigationResult,
    Recommendation,
    RootCause,
    SpecialistReport,
    WorkItemKind,
)
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from opsswarm.worker import ProcessorContext
from opsswarm.worker.processor import LangGraphMultiAgentProcessor

pytestmark = pytest.mark.asyncio


class UnusedParallelInvestigator:
    """路由校验失败时，任何图方法都不应被调用。"""

    async def has_checkpoint(self, *, thread_id: str) -> bool:
        del thread_id
        raise AssertionError("不应查询 checkpoint")

    async def investigate(
        self,
        request: InvestigationRequest,
        *,
        thread_id: str | None = None,
    ) -> MultiAgentInvestigationResult:
        del request, thread_id
        raise AssertionError("不应首次运行图")

    async def resume(self, *, thread_id: str) -> MultiAgentInvestigationResult:
        del thread_id
        raise AssertionError("不应恢复图")


def completed_live_result() -> MultiAgentInvestigationResult:
    """构造已经通过质疑节点的现场结果，不附带任何冻结场景真值。"""

    evidence = RELEASE_500_SCENARIO.evidence
    evidence_ids = tuple(item.evidence_id for item in evidence)
    artifacts = InvestigationArtifacts(
        result=InvestigationRunResult(
            conclusion=InvestigationConclusion(
                root_cause=RootCause.RELEASE_500_REGRESSION,
                confidence=0.96,
                summary="现场证据表明新版本导致订单接口大量返回 500。",
                evidence_ids=evidence_ids,
                recommendation=Recommendation.ROLLBACK_RELEASE,
                requires_human_approval=True,
            ),
            returned_evidence_ids=evidence_ids,
            tool_call_count=3,
        ),
        evidence_items=evidence,
    )
    reports = tuple(
        SpecialistReport(
            specialist=kind,
            summary=f"{kind.value} 专业调查完成。",
            findings=("发现支持当前根因判断的现场信号。",),
            evidence_ids=tuple(item.evidence_id for item in evidence if item.kind is kind),
            confidence=0.9,
        )
        for kind in (EvidenceKind.LOG, EvidenceKind.METRIC, EvidenceKind.CHANGE)
    )
    challenge = challenge_investigation(artifacts)
    return MultiAgentInvestigationResult(
        result=artifacts.result,
        evidence_items=evidence,
        specialist_reports=reports,
        hypothesis_challenge=challenge,
    )


class CompletedParallelInvestigator:
    """返回固定现场调查结果，用于验证评分边界。"""

    async def has_checkpoint(self, *, thread_id: str) -> bool:
        del thread_id
        return False

    async def investigate(
        self,
        request: InvestigationRequest,
        *,
        thread_id: str | None = None,
    ) -> MultiAgentInvestigationResult:
        del request, thread_id
        return completed_live_result()

    async def resume(self, *, thread_id: str) -> MultiAgentInvestigationResult:
        del thread_id
        raise AssertionError("首次执行不应恢复图")


async def test_multi_agent_processor_rejects_mismatched_work_item_kind() -> None:
    processor = LangGraphMultiAgentProcessor(
        investigator=UnusedParallelInvestigator(),
        model_name="unused-model",
    )
    context = ProcessorContext(
        request=InvestigationRequest(
            incident_id="00000000-0000-0000-0000-000000000001",
            service_name="order-service",
            title="订单接口异常",
            started_at="2026-01-01T08:00:00Z",
        ),
        mode=InvestigationMode.MULTI_AGENT,
        work_item_kind=WorkItemKind.RUN_SINGLE_AGENT_INVESTIGATION,
        graph_thread_id="investigation-run:00000000-0000-0000-0000-000000000001",
    )

    with pytest.raises(RuntimeError, match="调查运行模式与持久任务类型不匹配"):
        await processor.process(context)


async def test_live_processor_rejects_frozen_task_before_checkpoint_access() -> None:
    processor = LangGraphMultiAgentProcessor(
        investigator=UnusedParallelInvestigator(),
        model_name="unused-model",
        data_source_mode=InvestigationDataSourceMode.LIVE,
        evaluation_scenario=None,
    )
    context = ProcessorContext(
        request=InvestigationRequest(
            incident_id="00000000-0000-0000-0000-000000000001",
            service_name="order-service",
            title="订单接口异常",
            started_at="2026-01-01T08:00:00Z",
        ),
        mode=InvestigationMode.MULTI_AGENT,
        work_item_kind=WorkItemKind.RUN_MULTI_AGENT_INVESTIGATION,
        graph_thread_id="investigation-run:00000000-0000-0000-0000-000000000001",
        data_source_mode=InvestigationDataSourceMode.FROZEN,
    )

    with pytest.raises(RuntimeError, match="调查运行模式与持久任务类型不匹配"):
        await processor.process(context)


async def test_live_processor_persists_runtime_statistics_without_frozen_accuracy() -> None:
    processor = LangGraphMultiAgentProcessor(
        investigator=CompletedParallelInvestigator(),
        model_name="fake-live-model",
        data_source_mode=InvestigationDataSourceMode.LIVE,
        evaluation_scenario=None,
    )
    context = ProcessorContext(
        request=RELEASE_500_SCENARIO.request,
        mode=InvestigationMode.MULTI_AGENT,
        work_item_kind=WorkItemKind.RUN_MULTI_AGENT_INVESTIGATION,
        graph_thread_id="investigation-run:00000000-0000-0000-0000-000000000001",
        data_source_mode=InvestigationDataSourceMode.LIVE,
    )

    outcome = await processor.process(context)

    assert outcome.result.tool_call_count == 3
    assert outcome.metrics is None
