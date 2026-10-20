"""在 LangChain 每次真实模型和业务工具调用两侧接入预算与审计。"""

from collections.abc import Awaitable, Callable
from typing import Any
from uuid import UUID

from langchain.agents.middleware import AgentMiddleware, ModelRequest, ModelResponse
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage
from langchain_core.messages import ToolMessage as LangChainToolMessage
from langchain_core.outputs import ChatGeneration, LLMResult
from langgraph.prebuilt.tool_node import ToolCallRequest
from langgraph.types import Command

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy, ReportedModelUsage
from opsswarm.investigation.budget.runtime import BudgetedCallRunner
from opsswarm.investigation.loop.contracts import (
    AssistantMessage,
    EvidenceToolRequest,
    LoopModelInput,
    LoopModelResult,
    LoopToolResult,
    ReadEvidenceArguments,
    RunbookToolRequest,
    ToolMessage,
    ToolRequest,
)
from opsswarm.investigation.loop.messages import (
    from_langchain,
    model_result,
    reported_usage,
    validate_history,
)
from opsswarm.investigation.loop.policy import conflict, require_result_size, validate_model_result
from opsswarm.investigation.loop.service import EvidenceLoopService
from opsswarm.investigation.runbooks.contracts import ReadRunbookArguments


class UsageCapture(AsyncCallbackHandler):
    """解析失败发生在框架返回前，因此先从模型回调保留供应商用量。"""

    def __init__(self) -> None:
        self.raw: AIMessage | None = None

    async def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        del kwargs
        if len(response.generations) == 1 and len(response.generations[0]) == 1:
            generation = response.generations[0][0]
            if isinstance(generation, ChatGeneration) and isinstance(generation.message, AIMessage):
                self.raw = generation.message


class EvidenceLoopMiddleware(AgentMiddleware):
    def __init__(
        self,
        *,
        runner: BudgetedCallRunner,
        service: EvidenceLoopService,
        policy: ModelBudgetPolicy,
        role: str,
        capture: UsageCapture,
    ) -> None:
        self.runner, self.service, self.policy = runner, service, policy
        self.role, self.capture = role, capture
        self.index = 0
        self.source_call_id: UUID | None = None
        task_id, attempt_id = runner.current_task_id, runner.current_attempt_id
        if task_id is None or attempt_id is None:
            raise conflict("专业循环缺少运行层注入的任务尝试")
        self.task_id, self.attempt_id = task_id, attempt_id

    async def awrap_model_call(
        self,
        request: ModelRequest[Any],
        handler: Callable[[ModelRequest[Any]], Awaitable[ModelResponse[Any]]],
    ) -> ModelResponse[Any]:
        await self.runner.check()
        operation = CallOperation(f"analyze_{self.role}")
        packet = await self.service.prepare(
            self.task_id, self.attempt_id, operation, self.index, self.policy
        )
        inputs = packet.model_input
        assert isinstance(inputs, LoopModelInput)
        actual = from_langchain(
            [
                *([request.system_message] if request.system_message is not None else []),
                *request.messages,
            ]
        )
        validate_history(actual)
        if actual != inputs.messages:
            raise conflict("框架输入与持久交互重新组装的输入不一致")
        result: LoopModelResult | None = None
        usage: ReportedModelUsage | None = None
        self.capture.raw = None

        async def invoke() -> ModelResponse[Any]:
            nonlocal result, usage
            try:
                response = await handler(
                    request.override(
                        model_settings={
                            "max_completion_tokens": self.policy.max_output_tokens_per_call,
                            "parallel_tool_calls": False,
                        }
                    )
                )
            finally:
                if self.capture.raw is not None:
                    usage = reported_usage(self.capture.raw)
            normalized = model_result(response.result)
            history = tuple(
                m for m in inputs.messages if isinstance(m, (AssistantMessage, ToolMessage))
            )
            validate_model_result(
                normalized,
                self.role,
                history,
                allow_runbook="read_runbook" in packet.manifest.tool_names,
            )
            require_result_size(normalized)
            result = normalized
            return response

        response = await self.runner.execute(
            operation,
            invoke,
            model_input=inputs,
            context_manifest=packet.manifest,
            prompt_version=packet.manifest.prompt_version,
            invocation_index=self.index,
            usage_reader=lambda: usage,
            result_reader=lambda: result,
            reservation_observer=self._remember_call,
        )
        self.index += 1
        return response

    def _remember_call(self, call_id: UUID) -> None:
        self.source_call_id = call_id

    async def read_evidence(self, evidence_id: str, snapshot_sha256: str) -> str:
        if self.source_call_id is None:
            raise conflict("证据读取缺少已保存的模型来源")
        return await self.service.read(
            self.source_call_id,
            self.task_id,
            ReadEvidenceArguments(
                evidence_id=evidence_id,
                snapshot_sha256=snapshot_sha256,
            ),
        )

    async def read_runbook(self, runbook_id: str, version: str, content_sha256: str) -> str:
        if self.source_call_id is None:
            raise conflict("手册读取缺少已保存的模型来源")
        return await self.service.read_runbook(
            self.source_call_id,
            self.task_id,
            self.attempt_id,
            ReadRunbookArguments(
                runbook_id=runbook_id,
                version=version,
                content_sha256=content_sha256,
            ),
        )

    async def awrap_tool_call(
        self,
        request: ToolCallRequest,
        handler: Callable[[ToolCallRequest], Awaitable[LangChainToolMessage | Command[Any]]],
    ) -> LangChainToolMessage:
        tool_name = request.tool_call["name"]
        if (
            request.tool is None
            or tool_name not in ("read_evidence", "read_runbook")
            or request.tool_call["id"] is None
            or self.source_call_id is None
        ):
            raise conflict("未授权的业务工具")
        tool_request: ToolRequest
        if tool_name == "read_evidence":
            arguments = ReadEvidenceArguments.model_validate(request.tool_call["args"])
            tool_request = EvidenceToolRequest(
                source_call_id=self.source_call_id,
                tool_call_id=request.tool_call["id"],
                arguments=arguments,
            )
            operation = CallOperation(f"read_{self.role}_evidence")
        else:
            runbook_arguments = ReadRunbookArguments.model_validate(request.tool_call["args"])
            tool_request = RunbookToolRequest(
                source_call_id=self.source_call_id,
                tool_call_id=request.tool_call["id"],
                arguments=runbook_arguments,
            )
            operation = CallOperation(f"read_{self.role}_runbook")
        result: LoopToolResult | None = None

        async def invoke() -> LangChainToolMessage:
            nonlocal result
            response = await handler(request)
            if not isinstance(response, LangChainToolMessage) or response.status != "success":
                raise conflict("证据工具失败，停止当前尝试")
            normalized = from_langchain([response])[0]
            if not isinstance(normalized, ToolMessage):
                raise conflict("证据读取没有返回工具消息")
            validated = LoopToolResult(message=normalized)
            require_result_size(validated)
            result = validated
            return response

        return await self.runner.execute(
            operation,
            invoke,
            invocation_index=self.index - 1,
            tool_request=tool_request,
            result_reader=lambda: result,
        )
