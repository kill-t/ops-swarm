"""预算事务内验证工具来源、完整交互历史与有界返回内容。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.evidence_contracts import ContextEvidenceDetail
from opsswarm.investigation.context.loading import load_sources
from opsswarm.investigation.coordination.contracts import (
    CoordinationModelInput,
    CoordinationModelResult,
)
from opsswarm.investigation.loop.contracts import (
    TOOL_REQUEST_ADAPTER,
    AssistantMessage,
    CallResult,
    EvidenceToolRequest,
    LoopModelResult,
    LoopToolResult,
    ReadEvidenceArguments,
    RunbookToolRequest,
    ToolRequest,
)
from opsswarm.investigation.loop.history import saved_history as saved_history
from opsswarm.investigation.loop.policy import conflict, require_result_size, validate_model_result
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review.contracts import ReviewModelResult
from opsswarm.investigation.review.policy import validate_review_evidence
from opsswarm.investigation.runbooks.contracts import RunbookReadDetail
from opsswarm.investigation.runbooks.reader import RunbookReader
from opsswarm.investigation.synthesis.contracts import SynthesisModelResult
from opsswarm.investigation.synthesis.policy import validate_proposal_evidence
from opsswarm.investigation.tasks.tables import AgentTaskAttemptRow, AgentTaskRow

EVIDENCE_READ_OPERATIONS = frozenset(
    (
        CallOperation.READ_LOG_EVIDENCE,
        CallOperation.READ_METRIC_EVIDENCE,
        CallOperation.READ_CHANGE_EVIDENCE,
    )
)
RUNBOOK_READ_OPERATIONS = frozenset(
    (
        CallOperation.READ_LOG_RUNBOOK,
        CallOperation.READ_METRIC_RUNBOOK,
        CallOperation.READ_CHANGE_RUNBOOK,
    )
)
READ_OPERATIONS = EVIDENCE_READ_OPERATIONS | RUNBOOK_READ_OPERATIONS


async def validate_tool_request(
    session: AsyncSession,
    run: InvestigationRunRow,
    task_id: UUID | None,
    attempt_id: UUID | None,
    operation: CallOperation,
    invocation_index: int,
    request: ToolRequest | None,
) -> None:
    if operation not in READ_OPERATIONS:
        if request is not None:
            raise conflict("该操作不接受证据读取参数")
        return
    if request is None:
        raise conflict("受限读取缺少参数")
    evidence_read = operation in EVIDENCE_READ_OPERATIONS
    if evidence_read and (
        not isinstance(request, EvidenceToolRequest)
        or run.context_policy_version
        not in ("evidence-read-v1", "summary-context-v1", "runbook-read-v1")
    ):
        raise conflict("证据读取缺少受支持策略或参数")
    if not evidence_read and (
        not isinstance(request, RunbookToolRequest)
        or run.context_policy_version != "runbook-read-v1"
    ):
        raise conflict("手册读取缺少受支持策略或参数")
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
    if call.id != request.tool_call_id:
        raise conflict("工具参数与持久模型请求不一致")
    sources = await load_sources(session, run, operation, task_id)
    record = sources.tasks[0].evidence
    if isinstance(request, EvidenceToolRequest):
        if (
            call.name != "read_evidence"
            or ReadEvidenceArguments.model_validate(call.args) != request.arguments
            or request.arguments.snapshot_sha256 != record.content_sha256
            or request.arguments.evidence_id
            not in {e.evidence_id for e in record.snapshot.evidence}
        ):
            raise conflict("读取超出本任务目录或快照版本不一致")
    else:
        if call.name != "read_runbook" or call.args != request.arguments.model_dump(mode="json"):
            raise conflict("手册参数与持久模型请求不一致")
        RunbookReader(
            request=sources.request,
            task_evidence_record=record,
        ).read(request.arguments)


async def validate_call_result(
    session: AsyncSession,
    run: InvestigationRunRow,
    row: CallReservationRow,
    status: str,
    result: CallResult | None,
) -> None:
    if CallOperation(row.operation) == CallOperation.COORDINATE:
        try:
            model_input = CoordinationModelInput.model_validate(row.model_input)
        except ValueError as error:
            raise conflict("协调调用缺少有效模型输入") from error
        if (
            row.task_id is not None
            or row.task_attempt_id is not None
            or row.tool_request is not None
        ):
            raise conflict("协调调用必须属于运行根级")
        if result is not None:
            require_result_size(result)
        if status == "succeeded":
            if not isinstance(result, CoordinationModelResult):
                raise conflict("协调模型成功结算必须保存提案")
            if result.proposal.round_number != model_input.round_number:
                raise conflict("协调模型提案轮次与输入不一致")
        elif result is not None:
            raise conflict("失败或未知协调调用不能保存成功提案")
        return
    if CallOperation(row.operation) == CallOperation.SYNTHESIZE:
        if (
            row.task_id is not None
            or row.task_attempt_id is not None
            or row.tool_request is not None
        ):
            raise conflict("综合调用必须属于运行根级")
        if result is not None:
            require_result_size(result)
        if run.workflow_version != "v3":
            if result is not None:
                raise conflict("旧工作流综合调用不允许保存开放假设")
            return
        if status == "succeeded":
            if not isinstance(result, SynthesisModelResult):
                raise conflict("动态综合成功结算必须保存开放假设提案")
            sources = await load_sources(
                session,
                run,
                CallOperation.SYNTHESIZE,
                None,
                invocation_index=row.invocation_index,
            )
            validate_proposal_evidence(
                result.proposal,
                {
                    evidence.evidence_id
                    for task in sources.tasks
                    for evidence in task.evidence.snapshot.evidence
                },
            )
        elif result is not None:
            raise conflict("失败或未知动态综合调用不能保存成功提案")
        return
    if CallOperation(row.operation) == CallOperation.REVIEW:
        if (
            row.task_id is not None
            or row.task_attempt_id is not None
            or row.tool_request is not None
        ):
            raise conflict("独立审查调用必须属于运行根级")
        if result is not None:
            require_result_size(result)
        if run.workflow_version != "v3":
            raise conflict("独立审查只支持动态工作流")
        if status == "succeeded":
            if not isinstance(result, ReviewModelResult):
                raise conflict("独立审查成功结算必须保存结构化审查提案")
            sources = await load_sources(
                session,
                run,
                CallOperation.REVIEW,
                None,
                invocation_index=row.invocation_index,
            )
            if sources.open_hypothesis is None:
                raise conflict("独立审查缺少开放假设")
            validate_review_evidence(
                result.proposal,
                sources.open_hypothesis.hypothesis,
                {
                    evidence.evidence_id
                    for task in sources.tasks
                    for evidence in task.evidence.snapshot.evidence
                },
            )
        elif result is not None:
            raise conflict("失败或未知独立审查调用不能保存成功提案")
        return
    loop_model = row.model_input is not None and row.model_input.get("version") in (
        "evidence-loop-input-v1",
        "runbook-loop-input-v1",
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
        sources = await load_sources(session, run, CallOperation(row.operation), row.task_id)
        allow_runbook = False
        if run.context_policy_version == "runbook-read-v1":
            allow_runbook = bool(
                RunbookReader(
                    request=sources.request,
                    task_evidence_record=sources.tasks[0].evidence,
                ).catalog.entries
            )
        validate_model_result(
            result,
            task.assignment["role"],
            history,
            allow_runbook=allow_runbook,
        )
    else:
        if not isinstance(result, LoopToolResult):
            raise conflict("工具成功结算必须保存配对响应")
        request = TOOL_REQUEST_ADAPTER.validate_python(row.tool_request)
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
        if isinstance(request, EvidenceToolRequest):
            evidence = next(
                e
                for e in record.snapshot.evidence
                if e.evidence_id == request.arguments.evidence_id
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
            matches = (
                result.message.name == "read_evidence"
                and ContextEvidenceDetail.model_validate_json(result.message.content) == expected
            )
        else:
            document = RunbookReader(
                request=sources.request,
                task_evidence_record=record,
            ).read(request.arguments)
            expected_detail = RunbookReadDetail(
                investigation_run_id=run.id,
                call_id=request.source_call_id,
                task_id=record.task_id,
                document=document,
            )
            matches = (
                result.message.name == "read_runbook"
                and RunbookReadDetail.model_validate_json(result.message.content) == expected_detail
            )
        if result.message.tool_call_id != request.tool_call_id or not matches:
            raise conflict("工具返回与已提交材料、请求不一致")
