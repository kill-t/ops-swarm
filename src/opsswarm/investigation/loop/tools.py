"""唯一证据读取工具的固定 schema 与 LangChain 适配。"""

from collections.abc import Awaitable, Callable
from typing import Any

from langchain.agents.structured_output import OutputToolBinding, ToolStrategy
from langchain_core.tools import StructuredTool
from langchain_core.utils.function_calling import convert_to_openai_tool

from opsswarm.investigation.loop.contracts import ReadEvidenceArguments
from opsswarm.investigation.models import SpecialistReport

TOOL_DESCRIPTION = "读取本任务目录中的单条已保存证据。"


def evidence_tool(invoke: Callable[..., Awaitable[str]]) -> StructuredTool:
    return StructuredTool(
        name="read_evidence",
        description=TOOL_DESCRIPTION,
        args_schema=ReadEvidenceArguments,
        coroutine=invoke,
    )


def loop_tool_schema() -> dict[str, Any]:
    return {
        "tools": [
            convert_to_openai_tool(
                StructuredTool(
                    name="read_evidence",
                    description=TOOL_DESCRIPTION,
                    args_schema=ReadEvidenceArguments,
                )
            ),
            convert_to_openai_tool(
                OutputToolBinding.from_schema_spec(
                    ToolStrategy(SpecialistReport).schema_specs[0]
                ).tool
            ),
        ],
        # 锁定的 ChatOpenAI 将框架 ToolStrategy 的 any 规范化为 required。
        "tool_choice": "required",
        "parallel_tool_calls": False,
    }
