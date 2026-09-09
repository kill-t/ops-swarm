"""无网络与数据库依赖的循环结果、引用资格和返回大小规则。"""

import json

from opsswarm.investigation.context.evidence_contracts import ContextEvidenceDetail
from opsswarm.investigation.loop.contracts import (
    AssistantMessage,
    CallResult,
    LoopModelResult,
    ReadEvidenceArguments,
    ToolMessage,
)
from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.tasks.errors import TaskFactConflict


def conflict(message: str) -> TaskFactConflict:
    return TaskFactConflict("loop_interaction_conflict", message)


def require_result_size(result: CallResult) -> None:
    if (
        len(
            json.dumps(
                result.model_dump(mode="json"), ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
        )
        > 32768
    ):
        raise conflict("调用返回超过 32 KiB")


def validate_model_result(
    result: LoopModelResult, role: str, history: tuple[AssistantMessage | ToolMessage, ...]
) -> None:
    assistant = result.messages[0]
    if not isinstance(assistant, AssistantMessage) or len(assistant.tool_calls) != 1:
        raise conflict("专业模型必须返回一个受支持的工具请求")
    call = assistant.tool_calls[0]
    if any(
        isinstance(m, AssistantMessage) and any(c.id == call.id for c in m.tool_calls)
        for m in history
    ):
        raise conflict("同一尝试不能复用工具调用 ID")
    if call.name == "read_evidence":
        ReadEvidenceArguments.model_validate(call.args)
        if len(result.messages) != 1:
            raise conflict("业务工具响应必须由独立读取调用保存")
    elif call.name == "SpecialistReport":
        report = SpecialistReport.model_validate(call.args)
        read_ids = {
            ContextEvidenceDetail.model_validate_json(m.content).evidence.evidence_id
            for m in history
            if isinstance(m, ToolMessage) and m.name == "read_evidence"
        }
        if report.specialist != role or set(report.evidence_ids) - read_ids:
            raise conflict("报告只能引用当前专业尝试成功读取的证据")
        if len(result.messages) != 2:
            raise conflict("结构化输出缺少框架确认消息")
        receipt = result.messages[1]
        if (
            not isinstance(receipt, ToolMessage)
            or receipt.tool_call_id != call.id
            or receipt.name != call.name
        ):
            raise conflict("结构化输出确认消息不配对")
    else:
        raise conflict("专业模型请求了未授权工具")
