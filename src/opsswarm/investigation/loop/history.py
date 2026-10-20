"""从已结算账本重建完整配对历史与固定手册目录。"""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.loop.contracts import (
    AssistantMessage,
    LoopModelInput,
    LoopModelResult,
    LoopToolResult,
    ToolMessage,
)
from opsswarm.investigation.loop.policy import conflict
from opsswarm.investigation.runbooks.contracts import RunbookCatalog


async def saved_history(
    session: AsyncSession, run_id: UUID, attempt_id: UUID, invocation_index: int
) -> tuple[AssistantMessage | ToolMessage, ...]:
    rows = list(
        await session.scalars(
            select(CallReservationRow)
            .where(
                CallReservationRow.investigation_run_id == run_id,
                CallReservationRow.task_attempt_id == attempt_id,
            )
            .order_by(CallReservationRow.sequence)
        )
    )
    models = [r for r in rows if r.kind == "model" and r.invocation_index < invocation_index]
    if [r.invocation_index for r in models] != list(range(invocation_index)):
        raise conflict("模型调用槽位不连续")
    history: list[AssistantMessage | ToolMessage] = []
    for model in models:
        if model.status != "succeeded" or model.call_result is None:
            raise conflict("不能越过未成功保存的模型返回")
        result = LoopModelResult.model_validate(model.call_result)
        assistant = result.messages[0]
        if (
            len(result.messages) != 1
            or not isinstance(assistant, AssistantMessage)
            or len(assistant.tool_calls) != 1
            or assistant.tool_calls[0].name not in ("read_evidence", "read_runbook")
        ):
            raise conflict("不能在已结束的报告之后继续调用")
        matches = [
            r
            for r in rows
            if r.tool_request is not None and r.tool_request["source_call_id"] == str(model.id)
        ]
        if len(matches) != 1 or matches[0].status != "succeeded":
            raise conflict("模型工具请求缺少唯一成功响应")
        tool = LoopToolResult.model_validate(matches[0].call_result)
        if (
            tool.message.tool_call_id != assistant.tool_calls[0].id
            or tool.message.name != assistant.tool_calls[0].name
        ):
            raise conflict("模型请求与工具响应不配对")
        history.extend((assistant, tool.message))
    return tuple(history)


async def saved_runbook_catalog(
    session: AsyncSession, run_id: UUID, task_id: UUID
) -> RunbookCatalog | None:
    """同一任务的后续调用与技术重试复用首次目录。"""

    rows = list(
        await session.scalars(
            select(CallReservationRow)
            .where(
                CallReservationRow.investigation_run_id == run_id,
                CallReservationRow.task_id == task_id,
                CallReservationRow.kind == "model",
            )
            .order_by(CallReservationRow.sequence)
        )
    )
    if not rows:
        return None
    catalogs: list[RunbookCatalog] = []
    try:
        for row in rows:
            model_input = LoopModelInput.model_validate(row.model_input)
            if model_input.version != "runbook-loop-input-v1":
                raise ValueError
            payload = json.loads(model_input.messages[1].content)
            catalogs.append(RunbookCatalog.model_validate(payload["runbook_catalog"]))
    except (KeyError, TypeError, ValueError):
        raise conflict("已保存的调查手册目录无法校验") from None
    if any(item != catalogs[0] for item in catalogs[1:]):
        raise conflict("同一专业任务的调查手册目录发生漂移")
    return catalogs[0]
