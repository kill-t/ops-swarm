import asyncio
from collections.abc import Sequence

import pytest
from langchain_core.runnables import RunnableLambda

from opsswarm.investigation.errors import UnverifiedEvidenceReference
from opsswarm.investigation.evaluation import (
    compare_investigation_modes,
    evaluate_investigation,
)
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    HypothesisStatus,
    InvestigationConclusion,
    InvestigationRequest,
    InvestigationRunResult,
    SpecialistReport,
)
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.parallel_model import (
    LangChainConclusionSynthesizer,
    LangChainSpecialistAnalyst,
)
from opsswarm.investigation.scenarios import (
    RELEASE_500_SCENARIO,
    FrozenScenarioDataSource,
)


class ConcurrentSpecialistAnalyst:
    """通过并发计数证明三个 LangGraph 分支发生重叠执行。"""

    def __init__(self, *, invent_log_evidence: bool = False) -> None:
        self.active_count = 0
        self.max_active_count = 0
        self.invent_log_evidence = invent_log_evidence

    async def analyze(
        self,
        *,
        specialist: EvidenceKind,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
    ) -> SpecialistReport:
        del request
        self.active_count += 1
        self.max_active_count = max(self.max_active_count, self.active_count)
        try:
            await asyncio.sleep(0.02)
            evidence_ids = tuple(item.evidence_id for item in evidence)
            if specialist is EvidenceKind.LOG and self.invent_log_evidence:
                evidence_ids = ("invented-log-evidence",)
            return SpecialistReport(
                specialist=specialist,
                summary=f"{specialist.value} 专业节点完成分析。",
                findings=(f"已分析 {len(evidence)} 条证据。",),
                evidence_ids=evidence_ids,
                confidence=0.9,
            )
        finally:
            self.active_count -= 1


class RecordingSynthesizer:
    """记录扇入顺序并返回固定的正确综合结论。"""

    def __init__(self, *, invented_conclusion_evidence: bool = False) -> None:
        self.report_kinds: tuple[EvidenceKind, ...] = ()
        self.invented_conclusion_evidence = invented_conclusion_evidence

    async def synthesize(
        self,
        *,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
        reports: Sequence[SpecialistReport],
    ) -> InvestigationConclusion:
        del request
        self.report_kinds = tuple(item.specialist for item in reports)
        evidence_ids = tuple(item.evidence_id for item in evidence)
        if self.invented_conclusion_evidence:
            evidence_ids = ("invented-conclusion-evidence",)
        return InvestigationConclusion(
            root_cause="release_500_regression",
            confidence=0.97,
            summary="三类证据共同指向新版本回归。",
            evidence_ids=evidence_ids,
            recommendation="rollback_release",
            requires_human_approval=True,
            safety_notes=("只能在审批后由白名单执行器回滚。",),
        )


class StructuredModelStub:
    """返回固定结构化对象的模型适配桩。"""

    def with_structured_output(self, schema: type) -> RunnableLambda:
        if schema is SpecialistReport:
            response = {
                "specialist": "log",
                "summary": "日志报告",
                "findings": ["发现运行时异常"],
                "evidence_ids": ["log-order-runtime-error"],
                "confidence": 0.9,
            }
        else:
            response = {
                "root_cause": "release_500_regression",
                "confidence": 0.95,
                "summary": "新版本回归",
                "evidence_ids": ["log-order-runtime-error"],
                "recommendation": "rollback_release",
                "requires_human_approval": True,
                "safety_notes": ["需要审批"],
            }
        return RunnableLambda(lambda _: response)


class MissingLogDataSource(FrozenScenarioDataSource):
    """模拟日志平台没有返回任何结果。"""

    def query_logs(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        del service_name, lookback_minutes
        return ()


def build_investigator(
    *,
    analyst: ConcurrentSpecialistAnalyst | None = None,
    synthesizer: RecordingSynthesizer | None = None,
    data_source: FrozenScenarioDataSource | None = None,
) -> tuple[
    LangGraphParallelInvestigator,
    ConcurrentSpecialistAnalyst,
    RecordingSynthesizer,
]:
    selected_analyst = analyst or ConcurrentSpecialistAnalyst()
    selected_synthesizer = synthesizer or RecordingSynthesizer()
    return (
        LangGraphParallelInvestigator(
            data_source=data_source or FrozenScenarioDataSource(RELEASE_500_SCENARIO),
            analyst=selected_analyst,
            synthesizer=selected_synthesizer,
        ),
        selected_analyst,
        selected_synthesizer,
    )


@pytest.mark.asyncio
async def test_parallel_graph_fans_out_three_specialists_and_synthesizes() -> None:
    investigator, analyst, synthesizer = build_investigator()

    outcome = await investigator.investigate(RELEASE_500_SCENARIO.request)
    metrics = evaluate_investigation(outcome.result, RELEASE_500_SCENARIO)

    assert analyst.max_active_count == 3
    assert synthesizer.report_kinds == (
        EvidenceKind.CHANGE,
        EvidenceKind.LOG,
        EvidenceKind.METRIC,
    )
    assert tuple(report.specialist for report in outcome.specialist_reports) == (
        EvidenceKind.CHANGE,
        EvidenceKind.LOG,
        EvidenceKind.METRIC,
    )
    assert set(outcome.result.returned_evidence_ids) == RELEASE_500_SCENARIO.required_evidence_ids
    assert outcome.result.tool_call_count == 3
    assert outcome.hypothesis_challenge.status is HypothesisStatus.SUPPORTED
    assert metrics.top1_accuracy == 1
    assert metrics.evidence_recall == 1
    assert metrics.safety_violation_rate == 0

    mermaid = investigator.draw_mermaid()
    assert "coordinate" in mermaid
    assert "investigate_logs" in mermaid
    assert "investigate_metrics" in mermaid
    assert "investigate_changes" in mermaid
    assert "synthesize" in mermaid
    assert "challenge_hypothesis" in mermaid


@pytest.mark.asyncio
async def test_specialist_cannot_reference_evidence_outside_its_branch() -> None:
    investigator, _, _ = build_investigator(
        analyst=ConcurrentSpecialistAnalyst(invent_log_evidence=True)
    )

    with pytest.raises(UnverifiedEvidenceReference) as error:
        await investigator.investigate(RELEASE_500_SCENARIO.request)

    assert error.value.evidence_ids == frozenset({"invented-log-evidence"})


@pytest.mark.asyncio
async def test_synthesizer_cannot_reference_evidence_not_collected_by_graph() -> None:
    investigator, _, _ = build_investigator(
        synthesizer=RecordingSynthesizer(invented_conclusion_evidence=True)
    )

    with pytest.raises(UnverifiedEvidenceReference) as error:
        await investigator.investigate(RELEASE_500_SCENARIO.request)

    assert error.value.evidence_ids == frozenset({"invented-conclusion-evidence"})


@pytest.mark.asyncio
async def test_empty_specialist_source_still_reaches_synthesis_without_fake_evidence() -> None:
    investigator, _, _ = build_investigator(data_source=MissingLogDataSource(RELEASE_500_SCENARIO))

    outcome = await investigator.investigate(RELEASE_500_SCENARIO.request)
    metrics = evaluate_investigation(outcome.result, RELEASE_500_SCENARIO)
    log_report = next(
        report for report in outcome.specialist_reports if report.specialist is EvidenceKind.LOG
    )

    assert log_report.evidence_ids == ()
    assert "log-order-runtime-error" not in outcome.result.returned_evidence_ids
    assert metrics.evidence_recall == pytest.approx(2 / 3)


@pytest.mark.asyncio
async def test_single_and_multi_agent_use_the_same_evaluation_contract() -> None:
    investigator, _, _ = build_investigator()
    multi_agent = await investigator.investigate(RELEASE_500_SCENARIO.request)
    single_agent = InvestigationRunResult(
        conclusion=InvestigationConclusion(
            root_cause="release_500_regression",
            confidence=0.9,
            summary="单 Agent 找到发布与错误率证据。",
            evidence_ids=tuple(sorted(RELEASE_500_SCENARIO.required_evidence_ids)),
            recommendation="rollback_release",
            requires_human_approval=True,
        ),
        returned_evidence_ids=tuple(sorted(RELEASE_500_SCENARIO.required_evidence_ids)),
        tool_call_count=3,
    )

    comparison = compare_investigation_modes(
        single_agent,
        multi_agent.result,
        RELEASE_500_SCENARIO,
    )

    assert comparison.single_agent.top1_accuracy == 1
    assert comparison.multi_agent.top1_accuracy == 1
    assert comparison.evidence_recall_delta == 0
    assert comparison.tool_call_count_delta == 0
    assert comparison.safety_violation_rate_delta == 0


@pytest.mark.asyncio
async def test_langchain_model_adapters_validate_structured_responses() -> None:
    model = StructuredModelStub()
    analyst = LangChainSpecialistAnalyst(model)
    synthesizer = LangChainConclusionSynthesizer(model)
    log_evidence = [item for item in RELEASE_500_SCENARIO.evidence if item.kind is EvidenceKind.LOG]

    report = await analyst.analyze(
        specialist=EvidenceKind.LOG,
        request=RELEASE_500_SCENARIO.request,
        evidence=log_evidence,
    )
    conclusion = await synthesizer.synthesize(
        request=RELEASE_500_SCENARIO.request,
        evidence=log_evidence,
        reports=[report],
    )

    assert report.specialist is EvidenceKind.LOG
    assert report.evidence_ids == ("log-order-runtime-error",)
    assert conclusion.root_cause.value == "release_500_regression"
    assert conclusion.requires_human_approval is True
