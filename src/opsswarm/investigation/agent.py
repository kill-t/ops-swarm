"""基于 LangChain 的单 Agent 调查循环。"""

from langchain.agents import create_agent
from langchain.agents.structured_output import ToolStrategy
from langchain_core.language_models.chat_models import BaseChatModel

from opsswarm.investigation.errors import (
    MissingStructuredConclusion,
    UnverifiedEvidenceReference,
)
from opsswarm.investigation.models import (
    InvestigationConclusion,
    InvestigationRequest,
    InvestigationRunResult,
)
from opsswarm.investigation.tools import (
    EvidenceCollector,
    InvestigationDataSource,
    create_investigation_tools,
)

SYSTEM_PROMPT = """你是 OpsSwarm 的单 Agent 调查基线。
你只能使用提供的日志、指标和变更只读工具调查事故，禁止声称执行了 Shell、SQL、回滚或生产变更。
先收集相互独立的证据，再判断最可能根因；证据不足时必须选择 undetermined。
结论中的 evidence_ids 只能引用本轮工具真实返回的 evidence_id。
回滚等缓解动作只能作为建议，并且必须标记需要人工审批。
最终必须返回约定的结构化调查结论。"""


class SingleAgentInvestigator:
    """为每次调查创建隔离的工具统计器并校验证据引用。"""

    def __init__(
        self,
        *,
        model: BaseChatModel,
        data_source: InvestigationDataSource,
        recursion_limit: int = 12,
    ) -> None:
        if recursion_limit < 2:
            raise ValueError("recursion_limit 不能小于 2")
        self._model = model
        self._data_source = data_source
        self._recursion_limit = recursion_limit

    async def investigate(self, request: InvestigationRequest) -> InvestigationRunResult:
        """运行工具循环，并拒绝任何未经工具返回的证据引用。"""

        collector = EvidenceCollector()
        tools = create_investigation_tools(self._data_source, collector)
        workflow = create_agent(
            model=self._model,
            tools=tools,
            system_prompt=SYSTEM_PROMPT,
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

        return InvestigationRunResult(
            conclusion=conclusion,
            returned_evidence_ids=tuple(sorted(collector.returned_ids)),
            tool_call_count=collector.tool_call_count,
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
