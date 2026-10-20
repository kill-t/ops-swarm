"""唯一证据读取工具的固定 schema 与 LangChain 适配。"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.structured_output import OutputToolBinding, ToolStrategy
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from opsswarm.investigation.loop.contracts import ReadEvidenceArguments
from opsswarm.investigation.models import SpecialistReport
from opsswarm.investigation.runbooks.contracts import ReadRunbookArguments

TOOL_DESCRIPTION = "读取本任务目录中的单条已保存证据。"
RUNBOOK_TOOL_DESCRIPTION = "读取系统为当前任务选中的一个不可变调查手册版本。"


def evidence_tool(invoke: Callable[..., Awaitable[str]]) -> StructuredTool:
    return StructuredTool(
        name="read_evidence",
        description=TOOL_DESCRIPTION,
        args_schema=ReadEvidenceArguments,
        coroutine=invoke,
    )


def runbook_tool(invoke: Callable[..., Awaitable[str]]) -> StructuredTool:
    return StructuredTool(
        name="read_runbook",
        description=RUNBOOK_TOOL_DESCRIPTION,
        args_schema=ReadRunbookArguments,
        coroutine=invoke,
    )


def loop_tool_schema(*, include_runbook: bool = False) -> dict[str, Any]:
    tools = [
        convert_to_openai_tool(
            StructuredTool(
                name="read_evidence",
                description=TOOL_DESCRIPTION,
                args_schema=ReadEvidenceArguments,
            )
        )
    ]
    if include_runbook:
        tools.append(
            convert_to_openai_tool(
                StructuredTool(
                    name="read_runbook",
                    description=RUNBOOK_TOOL_DESCRIPTION,
                    args_schema=ReadRunbookArguments,
                )
            )
        )
    tools.append(
        convert_to_openai_tool(
            OutputToolBinding.from_schema_spec(ToolStrategy(SpecialistReport).schema_specs[0]).tool
        )
    )
    return {
        "tools": tools,
        # 锁定的 ChatOpenAI 将框架 ToolStrategy 的 any 规范化为 required。
        "tool_choice": "required",
        "parallel_tool_calls": False,
    }
