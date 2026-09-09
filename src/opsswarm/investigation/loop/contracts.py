"""独立版本的完整工具消息与持久交互契约，不改变旧输入序列化。"""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import ConfigDict, Field, JsonValue, TypeAdapter

from opsswarm.investigation.budget.model_contracts import ModelInput, TextModelMessage
from opsswarm.investigation.tasks.contracts import TaskModel

LOOP_POLICY_VERSION: Literal["evidence-read-v1"] = "evidence-read-v1"
LOOP_PROMPT_VERSION: Literal["evidence-read-v1"] = "evidence-read-v1"


class ReadEvidenceArguments(TaskModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)
    evidence_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class ToolCall(TaskModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)
    id: str = Field(min_length=1, max_length=100, pattern=r"^\S+$")
    name: str = Field(min_length=1, max_length=100)
    args: dict[str, JsonValue]


class AssistantMessage(TaskModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)
    role: Literal["assistant"] = "assistant"
    content: str = Field(max_length=32768)
    tool_calls: tuple[ToolCall, ...] = Field(max_length=1)


class ToolMessage(TaskModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)
    role: Literal["tool"] = "tool"
    content: str = Field(max_length=32768)
    tool_call_id: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=100)


LoopMessage = TextModelMessage | AssistantMessage | ToolMessage


class LoopModelInput(TaskModel):
    version: Literal["evidence-loop-input-v1"] = "evidence-loop-input-v1"
    messages: tuple[LoopMessage, ...] = Field(min_length=2, max_length=14)
    output_schema: dict[str, JsonValue]
    tool_schema: dict[str, JsonValue]
    structured_output_method: Literal["function_calling"] = "function_calling"


AnyModelInput = ModelInput | LoopModelInput
MODEL_INPUT_ADAPTER: TypeAdapter[AnyModelInput] = TypeAdapter(AnyModelInput)


class EvidenceToolRequest(TaskModel):
    source_call_id: UUID
    tool_call_id: str = Field(min_length=1, max_length=100)
    arguments: ReadEvidenceArguments


class LoopModelResult(TaskModel):
    kind: Literal["model"] = "model"
    messages: tuple[AssistantMessage | ToolMessage, ...] = Field(min_length=1, max_length=2)


class EvidenceToolResult(TaskModel):
    kind: Literal["tool"] = "tool"
    message: ToolMessage


CallResult = Annotated[LoopModelResult | EvidenceToolResult, Field(discriminator="kind")]
CALL_RESULT_ADAPTER: TypeAdapter[CallResult] = TypeAdapter(CallResult)


class CallInteraction(TaskModel):
    investigation_run_id: UUID
    call_id: UUID
    tool_request: EvidenceToolRequest | None
    result: CallResult | None
