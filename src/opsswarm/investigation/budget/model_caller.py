"""在完整应用输入边界组织 LangChain 结构化调用与报告用量。"""

import warnings
from collections.abc import Sequence

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, ValidationError

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import (
    ModelBudgetPolicy,
    ModelInput,
    ReportedModelUsage,
    TextModelMessage,
)
from opsswarm.investigation.budget.model_policy import allocate_input
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.prompt_versions import MULTI_AGENT_PROMPT_VERSION


class BudgetedModelCaller:
    def __init__(
        self, model: BaseChatModel, policy: ModelBudgetPolicy, runner: BudgetedCallRunner
    ) -> None:
        self._model = model
        self._policy = policy
        self._runner = runner

    async def invoke[T: BaseModel](
        self,
        schema: type[T],
        messages: Sequence[BaseMessage],
        operation: CallOperation,
    ) -> T:
        if any(
            not isinstance(m, (SystemMessage, HumanMessage)) or not isinstance(m.content, str)
            for m in messages
        ):
            raise CallBudgetExceeded("model_input_type_unsupported")
        if any(len(str(m.content)) > 262144 for m in messages):
            raise CallBudgetExceeded("model_input_too_large")
        model_input = ModelInput(
            messages=tuple(
                TextModelMessage(
                    role="system" if isinstance(m, SystemMessage) else "user",
                    content=str(m.content),
                )
                for m in messages
            ),
            output_schema=schema.model_json_schema(),
            tool_schema=convert_to_openai_tool(schema),
            structured_output_method=self._policy.profile.structured_output_method,
        )
        allocate_input(self._policy, model_input, prompt_version=MULTI_AGENT_PROMPT_VERSION)
        usage: ReportedModelUsage | None = None
        # 固定配置不允许兼容层静默替换输出方式；装配本身不发起请求。
        try:
            with warnings.catch_warnings():
                warnings.filterwarnings("error", message=r"(?s).*Overriding to method=.*")
                # 字典 Schema 保留原始消息，避免 SDK 的 Pydantic 解析先丢失 usage。
                structured = self._model.with_structured_output(
                    schema.model_json_schema(),
                    method=self._policy.profile.structured_output_method,
                    include_raw=True,
                    max_completion_tokens=self._policy.max_output_tokens_per_call,
                )
        except UserWarning:
            raise CallBudgetExceeded("model_budget_profile_mismatch") from None

        async def invoke() -> T:
            nonlocal usage
            response = await structured.ainvoke(list(messages))
            if not isinstance(response, dict):
                raise ValueError("模型没有返回包含原始消息的结构化结果")
            raw = response.get("raw")
            # 兼容层可能为缺字段的 usage 补零，必须核对供应商原始字段是否齐全。
            supplied = (
                raw.response_metadata.get("token_usage") if isinstance(raw, AIMessage) else None
            )
            if isinstance(supplied, dict) and all(
                supplied.get(key) is not None
                for key in ("prompt_tokens", "completion_tokens", "total_tokens")
            ):
                try:
                    usage = ReportedModelUsage.model_validate(
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
            return schema.model_validate(response.get("parsed"))

        return await self._runner.execute(
            operation,
            invoke,
            model_input=model_input,
            prompt_version=MULTI_AGENT_PROMPT_VERSION,
            usage_reader=lambda: usage,
        )
