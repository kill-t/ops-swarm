"""不访问网络的冻结模型，用于验证评测管线而非模型质量。"""

from collections.abc import Callable, Sequence
from typing import Any

from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage
from langchain_core.runnables import Runnable
from langchain_core.tools import BaseTool

from opsswarm.evaluation.dataset import MVP_INVESTIGATION_DATASET_V1
from opsswarm.evaluation.models import InvestigationBenchmarkReport
from opsswarm.evaluation.runner import run_investigation_benchmark
from opsswarm.investigation.agent import SingleAgentInvestigator
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationConclusion,
    InvestigationRequest,
    InvestigationRunResult,
    Recommendation,
    SpecialistReport,
)
from opsswarm.investigation.parallel_graph import LangGraphParallelInvestigator
from opsswarm.investigation.scenarios import FrozenScenarioDataSource, InvestigationScenario


class _ToolCallingFixtureModel(FakeMessagesListChatModel):
    """允许 LangChain 绑定工具的有限响应模型，不发起外部调用。"""

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Callable[..., Any] | BaseTool],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> Runnable[Any, AIMessage]:
        del tools, tool_choice, kwargs
        return self


class _FixtureSpecialistAnalyst:
    """把本分支冻结证据转换为结构化专业报告。"""

    async def analyze(
        self,
        *,
        specialist: EvidenceKind,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
    ) -> SpecialistReport:
        del request
        return SpecialistReport(
            specialist=specialist,
            summary=f"{specialist.value} 专业节点完成冻结证据分析。",
            findings=(f"读取到 {len(evidence)} 条 {specialist.value} 证据。",),
            evidence_ids=tuple(item.evidence_id for item in evidence),
            confidence=0.95,
        )


class _FixtureConclusionSynthesizer:
    """返回场景冻结期望，仅用于验证图、评分和报告契约。"""

    def __init__(self, scenario: InvestigationScenario) -> None:
        self._scenario = scenario

    async def synthesize(
        self,
        *,
        request: InvestigationRequest,
        evidence: Sequence[EvidenceObservation],
        reports: Sequence[SpecialistReport],
    ) -> InvestigationConclusion:
        del request, evidence, reports
        return _fixture_conclusion(self._scenario)


async def run_fixture_benchmark() -> InvestigationBenchmarkReport:
    """运行三故障离线契约基准，真实经过 LangChain 和 LangGraph。"""

    return await run_investigation_benchmark(
        MVP_INVESTIGATION_DATASET_V1,
        single_agent_runner=_run_single_agent,
        multi_agent_runner=_run_multi_agent,
    )


async def _run_single_agent(scenario: InvestigationScenario) -> InvestigationRunResult:
    investigator = SingleAgentInvestigator(
        model=_single_agent_fixture_model(scenario),
        data_source=FrozenScenarioDataSource(scenario),
    )
    return await investigator.investigate(scenario.request)


async def _run_multi_agent(scenario: InvestigationScenario) -> InvestigationRunResult:
    investigator = LangGraphParallelInvestigator(
        data_source=FrozenScenarioDataSource(scenario),
        analyst=_FixtureSpecialistAnalyst(),
        synthesizer=_FixtureConclusionSynthesizer(scenario),
    )
    return (await investigator.investigate(scenario.request)).result


def _single_agent_fixture_model(scenario: InvestigationScenario) -> _ToolCallingFixtureModel:
    service_name = scenario.request.service_name
    conclusion = _fixture_conclusion(scenario)
    return _ToolCallingFixtureModel(
        responses=[
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "query_service_logs",
                        "args": {"service_name": service_name, "lookback_minutes": 15},
                        "id": f"{scenario.scenario_id}-logs",
                        "type": "tool_call",
                    },
                    {
                        "name": "query_service_metrics",
                        "args": {"service_name": service_name, "lookback_minutes": 15},
                        "id": f"{scenario.scenario_id}-metrics",
                        "type": "tool_call",
                    },
                    {
                        "name": "list_recent_changes",
                        "args": {"service_name": service_name, "lookback_hours": 6},
                        "id": f"{scenario.scenario_id}-changes",
                        "type": "tool_call",
                    },
                ],
            ),
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "InvestigationConclusion",
                        "args": conclusion.model_dump(mode="json"),
                        "id": f"{scenario.scenario_id}-conclusion",
                        "type": "tool_call",
                    }
                ],
            ),
        ]
    )


def _fixture_conclusion(scenario: InvestigationScenario) -> InvestigationConclusion:
    requires_approval = scenario.expected_recommendation is Recommendation.ROLLBACK_RELEASE
    return InvestigationConclusion(
        root_cause=scenario.expected_root_cause,
        confidence=0.95,
        summary=f"冻结契约结论：{scenario.expected_root_cause.value}。",
        evidence_ids=tuple(sorted(scenario.required_evidence_ids)),
        recommendation=scenario.expected_recommendation,
        requires_human_approval=requires_approval,
        safety_notes=("回滚只能由工程师审批后的白名单执行器完成。",)
        if requires_approval
        else ("当前没有已授权处置动作，保持只读调查。",),
    )
