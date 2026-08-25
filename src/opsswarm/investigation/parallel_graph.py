"""LangGraph 并行专业调查工作流。"""

import operator
from collections.abc import Sequence
from typing import Annotated, NotRequired, Protocol, TypedDict, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from opsswarm.investigation.challenge import challenge_investigation
from opsswarm.investigation.errors import (
    IncompleteSpecialistReports,
    MissingStructuredConclusion,
    SpecialistKindMismatch,
    UnverifiedEvidenceReference,
)
from opsswarm.investigation.graph_thread import normalize_graph_thread_id
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    HypothesisChallenge,
    InvestigationArtifacts,
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
    hypothesis_challenge: NotRequired[HypothesisChallenge]


class _GraphUpdate(TypedDict, total=False):
    coordination_plan: tuple[EvidenceKind, ...]
    evidence: list[EvidenceObservation]
    reports: list[SpecialistReport]
    tool_call_count: int
    conclusion: InvestigationConclusion
    hypothesis_challenge: HypothesisChallenge


class LangGraphParallelInvestigator:
    """显式扇出三个专业节点，并在全部完成后综合结论。"""

    def __init__(
        self,
        *,
        data_source: InvestigationDataSource,
        analyst: SpecialistAnalyst,
        synthesizer: ConclusionSynthesizer,
        checkpointer: BaseCheckpointSaver[str] | None = None,
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
        builder.add_node("challenge_hypothesis", self._challenge_hypothesis)
        builder.add_edge(START, "coordinate")
        builder.add_edge("coordinate", "investigate_logs")
        builder.add_edge("coordinate", "investigate_metrics")
        builder.add_edge("coordinate", "investigate_changes")
        builder.add_edge(
            ["investigate_logs", "investigate_metrics", "investigate_changes"],
            "synthesize",
        )
        builder.add_edge("synthesize", "challenge_hypothesis")
        builder.add_edge("challenge_hypothesis", END)
        self._has_checkpointer = checkpointer is not None
        self._checkpointer = checkpointer
        self._workflow = builder.compile(
            checkpointer=checkpointer,
            name="parallel_investigation",
        )

    async def investigate(
        self,
        request: InvestigationRequest,
        *,
        thread_id: str | None = None,
    ) -> MultiAgentInvestigationResult:
        """运行并行图，并把非确定顺序的分支输出排序后返回。"""

        state = await self._workflow.ainvoke(
            {
                "request": request,
                "evidence": [],
                "reports": [],
                "tool_call_count": 0,
            },
            config=self._execution_config(thread_id),
        )
        return self._build_result(cast(_GraphState, state))

    async def resume(self, *, thread_id: str) -> MultiAgentInvestigationResult:
        """使用同一 thread_id 从 PostgreSQL 中最近的 checkpoint 继续。"""

        if not self._has_checkpointer:
            raise RuntimeError("当前并行调查图没有配置 checkpointer")
        state = await self._workflow.ainvoke(
            None,
            config=self._execution_config(thread_id),
        )
        return self._build_result(cast(_GraphState, state))

    async def has_checkpoint(self, *, thread_id: str) -> bool:
        """判断线程是否已经写入至少一个可恢复 checkpoint。"""

        if self._checkpointer is None:
            raise RuntimeError("当前并行调查图没有配置 checkpointer")
        config = self._execution_config(thread_id)
        return await self._checkpointer.aget_tuple(config) is not None

    def _execution_config(self, thread_id: str | None) -> RunnableConfig:
        config: RunnableConfig = {"max_concurrency": 3}
        if thread_id is not None:
            config["configurable"] = {"thread_id": normalize_graph_thread_id(thread_id)}
        elif self._has_checkpointer:
            raise ValueError("启用 checkpointer 后必须提供 thread_id")
        return config

    @staticmethod
    def _build_result(state: _GraphState) -> MultiAgentInvestigationResult:
        """从已完成或恢复后的图状态构造稳定业务结果。"""

        artifacts = LangGraphParallelInvestigator._build_artifacts(state)
        challenge = state.get("hypothesis_challenge")
        if challenge is None:
            raise MissingStructuredConclusion("并行调查图未返回根因假设质疑结果")
        return MultiAgentInvestigationResult(
            result=artifacts.result,
            evidence_items=artifacts.evidence_items,
            specialist_reports=artifacts.specialist_reports,
            hypothesis_challenge=challenge,
        )

    @staticmethod
    def _build_artifacts(state: _GraphState) -> InvestigationArtifacts:
        conclusion = state.get("conclusion")
        if conclusion is None:
            raise MissingStructuredConclusion("并行调查图未返回结构化结论")
        evidence = sorted(state["evidence"], key=lambda item: item.evidence_id)
        reports = sorted(state["reports"], key=lambda item: item.specialist.value)
        return InvestigationArtifacts(
            result=InvestigationRunResult(
                conclusion=conclusion,
                returned_evidence_ids=tuple(item.evidence_id for item in evidence),
                tool_call_count=state["tool_call_count"],
            ),
            evidence_items=tuple(evidence),
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

    @staticmethod
    def _challenge_hypothesis(state: _GraphState) -> _GraphUpdate:
        """使用确定性规则质疑综合结论，不再次调用模型。"""

        artifacts = LangGraphParallelInvestigator._build_artifacts(state)
        return {"hypothesis_challenge": challenge_investigation(artifacts)}
