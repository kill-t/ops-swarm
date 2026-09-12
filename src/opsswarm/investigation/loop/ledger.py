"""预算事务内验证工具来源、完整交互历史与有界返回内容。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.evidence_contracts import ContextEvidenceDetail
from opsswarm.investigation.context.loading import load_sources
from opsswarm.investigation.loop.contracts import (
    AssistantMessage,
    CallResult,
    EvidenceToolRequest,
    EvidenceToolResult,
    LoopModelResult,
    ReadEvidenceArguments,
)
from opsswarm.investigation.loop.history import saved_history as saved_history
from opsswarm.investigation.loop.policy import conflict, require_result_size, validate_model_result
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.tables import AgentTaskAttemptRow, AgentTaskRow

READ_OPERATIONS = frozenset(
    (
        CallOperation.READ_LOG_EVIDENCE,
        CallOperation.READ_METRIC_EVIDENCE,
        CallOperation.READ_CHANGE_EVIDENCE,
    )
)


async def validate_tool_request(
    session: AsyncSession,
    run: InvestigationRunRow,
    task_id: UUID | None,
    attempt_id: UUID | None,
    operation: CallOperation,
    invocation_index: int,
    request: EvidenceToolRequest | None,
) -> None:
    if operation not in READ_OPERATIONS:
        if request is not None:
            raise conflict("该操作不接受证据读取参数")
        return
    if request is None or run.context_policy_version not in (
        "evidence-read-v1",
        "summary-context-v1",
    ):
        raise conflict("证据读取缺少受支持策略或参数")
    source = await session.get(CallReservationRow, request.source_call_id)
    if (
        source is None
        or source.investigation_run_id != run.id
        or source.task_id != task_id
        or source.task_attempt_id != attempt_id
        or source.kind != "model"
        or source.status != "succeeded"
        or source.invocation_index != invocation_index
        or source.call_result is None
    ):
        raise conflict("工具请求必须来自当前尝试的已提交模型调用")
    result = LoopModelResult.model_validate(source.call_result)
    message = result.messages[0]
    if len(result.messages) != 1 or not isinstance(message, AssistantMessage):
        raise conflict("模型返回不是证据读取请求")
    call = message.tool_calls[0]
    if (
        call.name != "read_evidence"
        or call.id != request.tool_call_id
        or ReadEvidenceArguments.model_validate(call.args) != request.arguments
    ):
        raise conflict("工具参数与持久模型请求不一致")
    sources = await load_sources(session, run, operation, task_id)
    record = sources.tasks[0].evidence
    if (
        request.arguments.snapshot_sha256 != record.content_sha256
        or request.arguments.evidence_id not in {e.evidence_id for e in record.snapshot.evidence}
    ):
        raise conflict("读取超出本任务目录或快照版本不一致")


async def validate_call_result(
    session: AsyncSession,
    run: InvestigationRunRow,
    row: CallReservationRow,
    status: str,
    result: CallResult | None,
) -> None:
    loop_model = (
        row.model_input is not None and row.model_input.get("version") == "evidence-loop-input-v1"
    )
    if not loop_model and row.tool_request is None:
        if result is not None:
            raise conflict("历史调用不允许附加循环返回")
        return
    task = await session.get(AgentTaskRow, row.task_id)
    attempt = await session.get(AgentTaskAttemptRow, row.task_attempt_id)
    if task is None or attempt is None or task.status != "running" or attempt.status != "running":
        raise conflict("已失效的专业尝试不能写入迟到交互")
    if result is not None:
        require_result_size(result)
    if status != "succeeded":
        return
    if loop_model:
        if not isinstance(result, LoopModelResult):
            raise conflict("模型成功结算必须保存返回")
        assert row.task_attempt_id is not None
        history = await saved_history(session, run.id, row.task_attempt_id, row.invocation_index)
        validate_model_result(result, task.assignment["role"], history)
    else:
        if not isinstance(result, EvidenceToolResult):
            raise conflict("工具成功结算必须保存配对响应")
        request = EvidenceToolRequest.model_validate(row.tool_request)
        await validate_tool_request(
            session,
            run,
            row.task_id,
            row.task_attempt_id,
            CallOperation(row.operation),
            row.invocation_index,
            request,
        )
        sources = await load_sources(session, run, CallOperation(row.operation), row.task_id)
        record = sources.tasks[0].evidence
        evidence = next(
            e for e in record.snapshot.evidence if e.evidence_id == request.arguments.evidence_id
        )
        expected = ContextEvidenceDetail(
            investigation_run_id=run.id,
            call_id=request.source_call_id,
            task_id=record.task_id,
            version=record.snapshot.version,
            snapshot_id=record.snapshot.evidence_snapshot_id,
            snapshot_sha256=record.content_sha256,
            evidence=evidence,
        )
        if (
            result.message.tool_call_id != request.tool_call_id
            or result.message.name != "read_evidence"
            or ContextEvidenceDetail.model_validate_json(result.message.content) != expected
        ):
            raise conflict("工具返回与已提交证据、请求不一致")
