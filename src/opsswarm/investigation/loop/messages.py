"""在 LangChain 消息与可持久化工具循环消息之间做严格转换。"""

from collections.abc import Mapping, Sequence
from typing import Any

from langchain_core.messages import (
    AIMessage,
    AnyMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
)
from langchain_core.messages import ToolMessage as LangChainToolMessage

from opsswarm.investigation.budget.model_contracts import (
    ReportedModelUsage,
    TextModelMessage,
)
from opsswarm.investigation.loop.contracts import (
    AssistantMessage,
    LoopMessage,
    LoopModelResult,
    ReadEvidenceArguments,
    ToolCall,
    ToolMessage,
)

READ_EVIDENCE_TOOL_NAME = "read_evidence"
SPECIALIST_REPORT_TOOL_NAME = "SpecialistReport"
ALLOWED_TOOL_NAMES = frozenset({READ_EVIDENCE_TOOL_NAME, SPECIALIST_REPORT_TOOL_NAME})


def to_langchain(messages: Sequence[LoopMessage]) -> list[AnyMessage]:
    """校验完整历史，再恢复 LangChain 所需的消息对象。"""

    validate_history(messages)
    converted: list[AnyMessage] = []
    for message in messages:
        if isinstance(message, TextModelMessage):
            converted.append(
                SystemMessage(content=message.content)
                if message.role == "system"
                else HumanMessage(content=message.content)
            )
        elif isinstance(message, AssistantMessage):
            converted.append(
                AIMessage(
                    content=message.content,
                    tool_calls=[
                        {
                            "id": call.id,
                            "name": call.name,
                            "args": call.args,
                            "type": "tool_call",
                        }
                        for call in message.tool_calls
                    ],
                )
            )
        else:
            converted.append(
                LangChainToolMessage(
                    content=message.content,
                    tool_call_id=message.tool_call_id,
                    name=message.name,
                    status="success",
                )
            )
    return converted


def from_langchain(messages: Sequence[BaseMessage]) -> tuple[LoopMessage, ...]:
    """逐条规范化消息；完整历史的顺序由 ``validate_history`` 单独校验。"""

    return tuple(_from_langchain_message(message) for message in messages)


def model_result(messages: Sequence[BaseMessage]) -> LoopModelResult:
    """转换一次模型节点结果，区分读取请求与最终结构化报告。"""

    converted = tuple(_from_langchain_message(message) for message in messages)
    if not converted or len(converted) > 2:
        raise ValueError("模型结果必须包含一条 assistant 消息及可选的输出工具响应")
    assistant = converted[0]
    if not isinstance(assistant, AssistantMessage) or len(assistant.tool_calls) != 1:
        raise ValueError("模型结果必须以唯一工具调用的 assistant 消息开始")
    call = assistant.tool_calls[0]
    if len(converted) == 1:
        if call.name != READ_EVIDENCE_TOOL_NAME:
            raise ValueError("未配对的模型结果只能请求读取证据")
        return LoopModelResult(messages=(assistant,))
    else:
        response = converted[1]
        if call.name != SPECIALIST_REPORT_TOOL_NAME or not isinstance(response, ToolMessage):
            raise ValueError("双消息模型结果必须是最终专业报告及输出工具响应")
        _require_matching_tool_message(call, response)
        return LoopModelResult(messages=(assistant, response))


def reported_usage(raw: AIMessage) -> ReportedModelUsage | None:
    """仅接受供应商完整原始字段，避免把兼容层补零当成真实用量。"""

    token_usage = raw.response_metadata.get("token_usage")
    if isinstance(token_usage, dict):
        return _usage_from_mapping(
            token_usage,
            input_key="prompt_tokens",
            output_key="completion_tokens",
            total_key="total_tokens",
        )
    return None


def validate_history(messages: Sequence[LoopMessage]) -> None:
    """拒绝不能作为下一轮模型输入的顺序、重复 ID 和未配对工具消息。"""

    if len(messages) < 2:
        raise ValueError("工具循环历史至少需要 system 和 user 消息")
    if not (
        isinstance(messages[0], TextModelMessage)
        and messages[0].role == "system"
        and isinstance(messages[1], TextModelMessage)
        and messages[1].role == "user"
    ):
        raise ValueError("工具循环历史必须以 system、user 消息开始")

    seen_ids: set[str] = set()
    index = 2
    while index < len(messages):
        assistant = messages[index]
        if not isinstance(assistant, AssistantMessage) or len(assistant.tool_calls) != 1:
            raise ValueError("user 消息之后必须是包含唯一工具调用的 assistant 消息")
        call = assistant.tool_calls[0]
        _validate_call_args(call)
        if call.id in seen_ids:
            raise ValueError("工具调用 ID 不能重复")
        seen_ids.add(call.id)
        if index + 1 >= len(messages):
            raise ValueError("assistant 工具调用缺少配对的 tool 消息")
        response = messages[index + 1]
        if not isinstance(response, ToolMessage):
            raise ValueError("assistant 工具调用后必须紧跟配对的 tool 消息")
        _require_matching_tool_message(call, response)
        if call.name == SPECIALIST_REPORT_TOOL_NAME and index + 2 != len(messages):
            raise ValueError("最终专业报告之后不能继续追加消息")
        index += 2


def _from_langchain_message(message: BaseMessage) -> LoopMessage:
    content = _text_content(message)
    if isinstance(message, SystemMessage):
        return TextModelMessage(role="system", content=content)
    if isinstance(message, HumanMessage):
        return TextModelMessage(role="user", content=content)
    if isinstance(message, AIMessage):
        if message.invalid_tool_calls:
            raise ValueError("模型消息包含无法解析的工具调用")
        if len(message.tool_calls) != 1:
            raise ValueError("每次模型响应必须且只能包含一个工具调用")
        return AssistantMessage(
            content=content,
            tool_calls=tuple(_normalize_tool_call(call) for call in message.tool_calls),
        )
    if isinstance(message, LangChainToolMessage):
        if message.status != "success":
            raise ValueError("失败工具响应不能进入后续模型历史")
        if message.name is None:
            raise ValueError("工具响应缺少工具名称")
        _require_allowed_tool_name(message.name)
        return ToolMessage(
            content=content,
            tool_call_id=message.tool_call_id,
            name=message.name,
        )
    raise ValueError(f"不支持的 LangChain 消息类型：{type(message).__name__}")


def _normalize_tool_call(call: Mapping[str, Any]) -> ToolCall:
    name = call.get("name")
    call_id = call.get("id")
    args = call.get("args")
    if not isinstance(name, str) or not isinstance(call_id, str) or not isinstance(args, dict):
        raise ValueError("工具调用缺少有效的 name、id 或 args")
    _require_allowed_tool_name(name)
    if name == READ_EVIDENCE_TOOL_NAME:
        normalized_args = ReadEvidenceArguments.model_validate(args).model_dump(mode="json")
    else:
        # 最终输出的具体 Schema 由持久化服务按运行固定契约校验。
        normalized_args = args
    return ToolCall(id=call_id, name=name, args=normalized_args)


def _require_allowed_tool_name(name: str) -> None:
    if name not in ALLOWED_TOOL_NAMES:
        raise ValueError(f"不允许的工具：{name}")


def _require_matching_tool_message(call: ToolCall, response: ToolMessage) -> None:
    if response.tool_call_id != call.id or response.name != call.name:
        raise ValueError("tool 消息与 assistant 工具调用不匹配")


def _validate_call_args(call: ToolCall) -> None:
    _require_allowed_tool_name(call.name)
    if call.name == READ_EVIDENCE_TOOL_NAME:
        ReadEvidenceArguments.model_validate(call.args)


def _text_content(message: BaseMessage) -> str:
    if not isinstance(message.content, str):
        raise ValueError("工具循环只接受文本消息内容")
    return message.content


def _usage_from_mapping(
    usage: Mapping[str, Any], *, input_key: str, output_key: str, total_key: str
) -> ReportedModelUsage | None:
    values = (usage.get(input_key), usage.get(output_key), usage.get(total_key))
    if any(value is None for value in values):
        return None
    return ReportedModelUsage.model_validate(
        {
            "input_tokens": values[0],
            "output_tokens": values[1],
            "total_tokens": values[2],
        }
    )
