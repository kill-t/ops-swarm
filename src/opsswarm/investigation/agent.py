"""基于 LangChain 的单 Agent 调查循环。"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents import create_agent
from langchain.agents.middleware import (
    AgentMiddleware,
    ModelRequest,
    ModelResponse,
    wrap_model_call,
)
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel
from opentelemetry import trace
from opentelemetry.trace import Tracer

from opsswarm.investigation.errors import (
    MissingStructuredConclusion,
    UnverifiedEvidenceReference,
)
from opsswarm.investigation.models import (
    InvestigationArtifacts,
    InvestigationConclusion,
    InvestigationRequest,
    InvestigationRunResult,
)
from opsswarm.investigation.tools import (
    INVESTIGATION_TOOL_NAMES,
    EvidenceCollector,
    InvestigationDataSource,
    create_investigation_tools,
)

SYSTEM_PROMPT = """你是 OpsSwarm 的单 Agent 调查基线。
你只能使用提供的日志、指标和变更只读工具调查事故，禁止声称执行了 Shell、SQL、回滚或生产变更。
先收集相互独立的证据，再判断最可能根因；证据不足时必须选择 undetermined。
结论中的 evidence_ids 只能引用本轮工具真实返回的 evidence_id。
回滚等缓解动作只能作为建议，并且必须标记需要人工审批。
建议必须按根因选择：版本回归用 rollback_release；配置错误用 rollback_configuration；
依赖不可用用 restore_dependency；证据不足或没有明确受控动作时用 continue_investigation。
最终必须返回约定的结构化调查结论。"""

TOOL_POLICY_PROMPT = """日志、指标和最近变更工具各调用一次，并优先在第一轮并行调用。
工具返回的是当前调查时间窗的完整只读快照，不要重复查询同一个工具。
收到三类工具结果后立即提交结构化调查结论，不再调用调查工具。"""


class SingleAgentInvestigator:
    """为每次调查创建隔离的工具统计器并校验证据引用。"""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        data_source: InvestigationDataSource,
        recursion_limit: int = 12,
        max_tool_calls: int | None = 6,
        tracer: Tracer | None = None,
    ) -> None:
        if recursion_limit < 2:
            raise ValueError("recursion_limit 不能小于 2")
        self._model = model
        self._data_source = data_source
        self._recursion_limit = recursion_limit
        self._max_tool_calls = max_tool_calls
        self._tracer = tracer or trace.get_tracer(__name__)

    async def investigate(self, request: InvestigationRequest) -> InvestigationRunResult:
        """运行工具循环，并拒绝任何未经工具返回的证据引用。"""

        return (await self.investigate_with_evidence(request)).result

    async def investigate_with_evidence(
        self,
        request: InvestigationRequest,
    ) -> InvestigationArtifacts:
        """返回结论和工具真实观察，供 Worker 原子写入 Evidence Board。"""

        collector = EvidenceCollector(max_tool_calls=self._max_tool_calls)
        tools = create_investigation_tools(
            self._data_source,
            collector,
            tracer=self._tracer,
        )
        workflow = create_agent(
            model=self._model,
            tools=tools,
            system_prompt=f"{SYSTEM_PROMPT}\n{TOOL_POLICY_PROMPT}",
            middleware=(_force_conclusion_after_complete_evidence(collector),),
            response_format=ToolStrategy(
                InvestigationConclusion,
                handle_errors=False,
            ),
            name="single_investigator",
        )
        state = await workflow.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": self._build_user_prompt(request),
                    }
                ]
            },
            config={"recursion_limit": self._recursion_limit},
        )

        structured_response = state.get("structured_response")
        if structured_response is None:
            raise MissingStructuredConclusion("模型未返回结构化调查结论")
        conclusion = (
            structured_response
            if isinstance(structured_response, InvestigationConclusion)
            else InvestigationConclusion.model_validate(structured_response)
        )

        unknown_ids = set(conclusion.evidence_ids) - collector.returned_ids
        if unknown_ids:
            raise UnverifiedEvidenceReference(unknown_ids)

        return InvestigationArtifacts(
            result=InvestigationRunResult(
                conclusion=conclusion,
                returned_evidence_ids=tuple(sorted(collector.returned_ids)),
                tool_call_count=collector.tool_call_count,
            ),
            evidence_items=collector.evidence_items,
        )

    @staticmethod
    def _build_user_prompt(request: InvestigationRequest) -> str:
        return (
            f"调查事故 {request.incident_id}。\n"
            f"服务：{request.service_name}\n"
            f"标题：{request.title}\n"
            f"开始时间：{request.started_at.isoformat()}\n"
            "请查询日志、指标和最近变更，并给出带证据的最可能根因。"
        )


def _force_conclusion_after_complete_evidence(
    collector: EvidenceCollector,
) -> AgentMiddleware[Any, Any, InvestigationConclusion]:
    """三类现场快照齐备后只保留结构化结论工具，阻止重复外部查询。"""

    @wrap_model_call(name="ForceInvestigationConclusion")
    async def force_conclusion(
        request: ModelRequest[Any],
        handler: Callable[
            [ModelRequest[Any]],
            Awaitable[ModelResponse[InvestigationConclusion]],
        ],
    ) -> ModelResponse[InvestigationConclusion]:
        if collector.called_tool_names >= INVESTIGATION_TOOL_NAMES:
            request = request.override(tools=[], tool_choice="required")
        return await handler(request)

    return force_conclusion
