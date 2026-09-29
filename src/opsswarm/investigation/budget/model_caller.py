"""在完整应用输入边界组织 LangChain 结构化调用与报告用量。"""

import warnings
from collections.abc import Callable, Sequence
from uuid import UUID

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
from opsswarm.investigation.context.contracts import ContextPacket
from opsswarm.investigation.context.service import TaskContextService
from opsswarm.investigation.coordination.contracts import CoordinationModelInput
from opsswarm.investigation.loop.contracts import AnyModelInput, CallResult
from opsswarm.investigation.prompt_versions import MULTI_AGENT_PROMPT_VERSION


class BudgetedModelCaller:
    def __init__(
        self,
        model: BaseChatModel,
        policy: ModelBudgetPolicy,
        runner: BudgetedCallRunner,
        *,
        context_service: TaskContextService | None = None,
    ) -> None:
        self._model = model
        self._policy = policy
        self._runner = runner
        self._context_service = context_service

    async def invoke[T: BaseModel](
        self,
        schema: type[T],
        messages: Sequence[BaseMessage],
        operation: CallOperation,
        *,
        prepared_packet: ContextPacket | None = None,
        result_factory: Callable[[T], CallResult] | None = None,
        reservation_observer: Callable[[UUID], None] = lambda _: None,
        invocation_index: int = 0,
    ) -> T:
        if any(
            not isinstance(m, (SystemMessage, HumanMessage)) or not isinstance(m.content, str)
            for m in messages
        ):
            raise CallBudgetExceeded("model_input_type_unsupported")
        if any(len(str(m.content)) > 262144 for m in messages):
            raise CallBudgetExceeded("model_input_too_large")
        model_input: AnyModelInput = ModelInput(
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
        prompt_version = MULTI_AGENT_PROMPT_VERSION
        manifest = None
        if prepared_packet is not None:
            if self._context_service is not None:
                raise CallBudgetExceeded("model_context_source_conflict")
            if (
                not isinstance(prepared_packet.model_input, (CoordinationModelInput, ModelInput))
                or prepared_packet.model_input.output_schema != schema.model_json_schema()
                or prepared_packet.model_input.tool_schema != convert_to_openai_tool(schema)
                or prepared_packet.model_input.structured_output_method
                != self._policy.profile.structured_output_method
            ):
                raise CallBudgetExceeded("model_input_schema_mismatch")
            model_input = prepared_packet.model_input
            manifest = prepared_packet.manifest
            prompt_version = manifest.prompt_version
            messages = [
                SystemMessage(content=m.content)
                if m.role == "system"
                else HumanMessage(content=m.content)
                for m in model_input.messages
            ]
        elif self._context_service is not None:
            if not isinstance(model_input, ModelInput):
                raise CallBudgetExceeded("model_input_type_unsupported")
            await self._runner.check()
            packet = await self._context_service.prepare(
                model_input,
                operation,
                self._runner.current_task_id,
                self._policy,
                invocation_index,
            )
            if not isinstance(packet.model_input, ModelInput):
                raise CallBudgetExceeded("model_input_type_unsupported")
            model_input, manifest = packet.model_input, packet.manifest
            prompt_version = manifest.prompt_version
            messages = [
                SystemMessage(content=m.content)
                if m.role == "system"
                else HumanMessage(content=m.content)
                for m in model_input.messages
            ]
        allocate_input(self._policy, model_input, prompt_version=prompt_version)
        usage: ReportedModelUsage | None = None
        call_result: CallResult | None = None
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
            nonlocal call_result, usage
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
            parsed = schema.model_validate(response.get("parsed"))
            if result_factory is not None:
                call_result = result_factory(parsed)
            return parsed

        return await self._runner.execute(
            operation,
            invoke,
            model_input=model_input,
            prompt_version=prompt_version,
            context_manifest=manifest,
            result_reader=lambda: call_result,
            reservation_observer=reservation_observer,
            usage_reader=lambda: usage,
            invocation_index=invocation_index,
        )
