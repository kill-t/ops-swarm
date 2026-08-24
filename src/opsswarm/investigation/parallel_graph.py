"""LangGraph 并行专业调查工作流。"""

import operator
from collections.abc import Sequence
from typing import Annotated, NotRequired, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph

from opsswarm.investigation.errors import (
    IncompleteSpecialistReports,
    MissingStructuredConclusion,
    SpecialistKindMismatch,
    UnverifiedEvidenceReference,
)
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationConclusion,
    InvestigationRequest,
    InvestigationRunResult,
    MultiAgentInvestigationResult,
    SpecialistReport,
)
from opsswarm.investigation.tools import InvestigationDataSource

_SPECIALIST_KINDS = (EvidenceKind.LOG, EvidenceKind.METRIC, EvidenceKind.CHANGE)


class SpecialistAnalyst(Protocol):
    """专业 Agent 的结构化分析端口。"""

    async def analyze(
        self,
        *,
        specialist: EvidenceKind,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
    ) -> SpecialistReport:
        """只分析分配给本专业节点的证据。"""


class ConclusionSynthesizer(Protocol):
    """把多份专业报告综合为最终根因的 Agent 端口。"""

    async def synthesize(
        self,
        *,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
        reports: Sequence[SpecialistReport],
    ) -> InvestigationConclusion:
        """生成带证据引用的结构化综合结论。"""


class _GraphState(TypedDict):
    request: InvestigationRequest
    coordination_plan: NotRequired[tuple[EvidenceKind, ...]]
    evidence: Annotated[list[EvidenceObservation], operator.add]
    reports: Annotated[list[SpecialistReport], operator.add]
    tool_call_count: Annotated[int, operator.add]
    conclusion: NotRequired[InvestigationConclusion]


class _GraphUpdate(TypedDict, total=False):
    coordination_plan: tuple[EvidenceKind, ...]
    evidence: list[EvidenceObservation]
    reports: list[SpecialistReport]
    tool_call_count: int
    conclusion: InvestigationConclusion


class LangGraphParallelInvestigator:
    """显式扇出三个专业节点，并在全部完成后综合结论。"""

    def __init__(
        self,
        *,
        data_source: InvestigationDataSource,
        analyst: SpecialistAnalyst,
        synthesizer: ConclusionSynthesizer,
    ) -> None:
        self._data_source = data_source
        self._analyst = analyst
        self._synthesizer = synthesizer
        builder = StateGraph(_GraphState)
        builder.add_node("coordinate", self._coordinate)
        builder.add_node("investigate_logs", self._investigate_logs)
        builder.add_node("investigate_metrics", self._investigate_metrics)
        builder.add_node("investigate_changes", self._investigate_changes)
        builder.add_node("synthesize", self._synthesize)
        builder.add_edge(START, "coordinate")
        builder.add_edge("coordinate", "investigate_logs")
        builder.add_edge("coordinate", "investigate_metrics")
        builder.add_edge("coordinate", "investigate_changes")
        builder.add_edge(
            ["investigate_logs", "investigate_metrics", "investigate_changes"],
            "synthesize",
        )
        builder.add_edge("synthesize", END)
        self._workflow = builder.compile(name="parallel_investigation")

    async def investigate(self, request: InvestigationRequest) -> MultiAgentInvestigationResult:
        """运行并行图，并把非确定顺序的分支输出排序后返回。"""

        state = await self._workflow.ainvoke(
            {
                "request": request,
                "evidence": [],
                "reports": [],
                "tool_call_count": 0,
            },
            config={"max_concurrency": 3},
        )
        conclusion = state.get("conclusion")
        if conclusion is None:
            raise MissingStructuredConclusion("并行调查图未返回结构化结论")
        evidence = sorted(state["evidence"], key=lambda item: item.evidence_id)
        reports = sorted(state["reports"], key=lambda item: item.specialist.value)
        return MultiAgentInvestigationResult(
            result=InvestigationRunResult(
                conclusion=conclusion,
                returned_evidence_ids=tuple(item.evidence_id for item in evidence),
                tool_call_count=state["tool_call_count"],
            ),
            specialist_reports=tuple(reports),
        )

    def draw_mermaid(self) -> str:
        """输出可用于架构文档和调试的实际编译图。"""

        return self._workflow.get_graph().draw_mermaid()

    @staticmethod
    def _coordinate(state: _GraphState) -> _GraphUpdate:
        del state
        return {"coordination_plan": _SPECIALIST_KINDS}

    async def _investigate_logs(self, state: _GraphState) -> _GraphUpdate:
        evidence = self._data_source.query_logs(
            service_name=state["request"].service_name,
            lookback_minutes=15,
        )
        return await self._analyze(EvidenceKind.LOG, state, evidence)

    async def _investigate_metrics(self, state: _GraphState) -> _GraphUpdate:
        evidence = self._data_source.query_metrics(
            service_name=state["request"].service_name,
            lookback_minutes=15,
        )
        return await self._analyze(EvidenceKind.METRIC, state, evidence)

    async def _investigate_changes(self, state: _GraphState) -> _GraphUpdate:
        evidence = self._data_source.list_recent_changes(
            service_name=state["request"].service_name,
            lookback_hours=6,
        )
        return await self._analyze(EvidenceKind.CHANGE, state, evidence)

    async def _analyze(
        self,
        specialist: EvidenceKind,
        state: _GraphState,
        evidence: Sequence[EvidenceObservation],
    ) -> _GraphUpdate:
        plan = state.get("coordination_plan", ())
        if specialist not in plan:
            raise SpecialistKindMismatch(f"协调计划没有包含专业节点：{specialist.value}")
        report = await self._analyst.analyze(
            specialist=specialist,
            request=state["request"],
            evidence=evidence,
        )
        if report.specialist is not specialist:
            raise SpecialistKindMismatch(
                f"{specialist.value} 节点返回了 {report.specialist.value} 报告"
            )
        returned_ids = {item.evidence_id for item in evidence}
        unknown_ids = set(report.evidence_ids) - returned_ids
        if unknown_ids:
            raise UnverifiedEvidenceReference(unknown_ids)
        return {
            "evidence": list(evidence),
            "reports": [report],
            "tool_call_count": 1,
        }

    async def _synthesize(self, state: _GraphState) -> _GraphUpdate:
        reports = sorted(state["reports"], key=lambda item: item.specialist.value)
        if {item.specialist for item in reports} != set(_SPECIALIST_KINDS):
            raise IncompleteSpecialistReports("综合节点必须收到日志、指标和变更三份报告")
        evidence = sorted(state["evidence"], key=lambda item: item.evidence_id)
        conclusion = await self._synthesizer.synthesize(
            request=state["request"],
            evidence=evidence,
            reports=reports,
        )
        unknown_ids = set(conclusion.evidence_ids) - {item.evidence_id for item in evidence}
        if unknown_ids:
            raise UnverifiedEvidenceReference(unknown_ids)
        return {"conclusion": conclusion}
