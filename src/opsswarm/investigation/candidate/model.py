"""将完整候选评估输入交给结构化模型，并保留供应商报告用量。"""

import warnings

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ValidationError

from opsswarm.investigation.budget.model_contracts import (
    ModelBudgetPolicy,
    ModelInput,
    ReportedModelUsage,
)
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.candidate.output import CandidateModelResult, CandidateProposal


class CandidateModel:
    """只负责一次结构化调用；预算、重试和证据闭包由调用方管理。"""

    def __init__(self, model: BaseChatModel, policy: ModelBudgetPolicy) -> None:
        self._model = model
        self._policy = policy
        self.usage: ReportedModelUsage | None = None

    async def invoke(self, model_input: ModelInput) -> CandidateModelResult:
        # 失败调用也不能沿用上次成功用量，解析失败则保留本次已取得的用量。
        self.usage = None
        if (
            self._policy.profile.structured_output_method != "function_calling"
            or model_input.structured_output_method != "function_calling"
        ):
            raise CallBudgetExceeded("model_budget_profile_mismatch")
        if (
            model_input.output_schema != CandidateProposal.model_json_schema()
            or model_input.tool_schema != convert_to_openai_tool(CandidateProposal)
        ):
            raise CallBudgetExceeded("model_input_schema_mismatch")
        messages = [
            SystemMessage(content=message.content)
            if message.role == "system"
            else HumanMessage(content=message.content)
            for message in model_input.messages
        ]
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("error", message=r"(?s).*Overriding to method=.*")
                # 直接使用已冻结的 OpenAI 工具 Schema，避免 SDK 再次转换改变完整输入。
                structured = self._model.with_structured_output(
                    model_input.tool_schema,
                    method="function_calling",
                    include_raw=True,
                    max_completion_tokens=self._policy.max_output_tokens_per_call,
                )
        except UserWarning:
            raise CallBudgetExceeded("model_budget_profile_mismatch") from None
        response = await structured.ainvoke(messages)
        if not isinstance(response, dict):
            raise ValueError("模型没有返回包含原始消息的结构化结果")
        raw = response.get("raw")
        supplied = raw.response_metadata.get("token_usage") if isinstance(raw, AIMessage) else None
        # SDK 可能补齐缺失字段，只有供应商原始三字段齐全才承认报告用量。
        if isinstance(supplied, dict) and all(
            supplied.get(key) is not None
            for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        ):
            try:
                self.usage = ReportedModelUsage.model_validate(
                    {
                        "input_tokens": supplied["prompt_tokens"],
                        "output_tokens": supplied["completion_tokens"],
                        "total_tokens": supplied["total_tokens"],
                    }
                )
            except ValidationError:
                raise CallBudgetExceeded("model_usage_invalid") from None
        if response.get("parsing_error") is not None:
            raise ValueError("模型结构化输出解析失败")
        proposal = CandidateProposal.model_validate(response.get("parsed"))
        return CandidateModelResult(proposal=proposal)
