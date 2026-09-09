"""在任务结算事务内保存摘要，并在读取时复核不可变业务来源。"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.budget.contracts import CallReservationRecord
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.loop.contracts import (
    AssistantMessage,
    EvidenceToolRequest,
    LoopModelResult,
)
from opsswarm.investigation.loop.history import saved_history
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.evidence import TaskEvidenceRecord
from opsswarm.investigation.tasks.evidence_tables import AgentTaskEvidenceRow
from opsswarm.investigation.tasks.results import TaskResultRecord
from opsswarm.investigation.tasks.tables import AgentTaskRow
from opsswarm.investigation.working_summary.builder import build_summary
from opsswarm.investigation.working_summary.contracts import (
    SUMMARY_VERSION,
    TaskWorkingSummary,
    WorkingSummaryRecord,
)
from opsswarm.investigation.working_summary.hashing import material_hash
from opsswarm.investigation.working_summary.tables import AgentTaskWorkingSummaryRow


async def expected_summary(
    session: AsyncSession,
    task: AgentTaskRow,
    result: TaskResultRecord,
) -> TaskWorkingSummary:
    evidence = await session.get(AgentTaskEvidenceRow, task.id)
    calls = list(
        await session.scalars(
            select(CallReservationRow)
            .where(
                CallReservationRow.investigation_run_id == task.investigation_run_id,
                CallReservationRow.task_id == task.id,
            )
            .order_by(CallReservationRow.sequence)
            .limit(161)
        )
    )
    models = [c for c in calls if c.kind == "model" and c.task_attempt_id == result.attempt_id]
    if evidence is None or not models or len(calls) > 160:
        raise TaskFactConflict("working_summary_source_invalid", "摘要缺少有界证据或最终模型调用")
    final = max(models, key=lambda c: c.invocation_index)
    try:
        history = await saved_history(
            session, task.investigation_run_id, result.attempt_id, final.invocation_index
        )
        for call in calls:
            if call.task_attempt_id != result.attempt_id or call.tool_request is None:
                continue
            request = EvidenceToolRequest.model_validate(call.tool_request)
            source = next((m for m in models if m.id == request.source_call_id), None)
            if source is None or source.invocation_index != call.invocation_index:
                raise ValueError("工具调用来源不匹配")
            message = LoopModelResult.model_validate(source.call_result).messages[0]
            if (
                not isinstance(message, AssistantMessage)
                or len(message.tool_calls) != 1
                or message.tool_calls[0].id != request.tool_call_id
                or message.tool_calls[0].name != "read_evidence"
                or message.tool_calls[0].args != request.arguments.model_dump(mode="json")
            ):
                raise ValueError("读取参数与持久模型请求不一致")
        return build_summary(
            result,
            TaskEvidenceRecord.model_validate(evidence, from_attributes=True),
            TaskAssignment.model_validate(task.assignment),
            tuple(CallReservationRecord.model_validate(c, from_attributes=True) for c in calls),
            CallReservationRecord.model_validate(final, from_attributes=True),
            LoopModelResult.model_validate(final.call_result),
            history,
        )
    except ValueError as error:
        raise TaskFactConflict("working_summary_source_invalid", "工作摘要来源校验失败") from error


async def save_summary(
    session: AsyncSession,
    task: AgentTaskRow,
    result: TaskResultRecord,
) -> None:
    run = await session.get(InvestigationRunRow, task.investigation_run_id)
    assert run is not None
    if run.working_summary_policy_version is None:
        return
    if run.working_summary_policy_version != SUMMARY_VERSION:
        raise TaskFactConflict("working_summary_policy_invalid", "不支持的工作摘要策略")
    summary = await expected_summary(session, task, result)
    session.add(
        AgentTaskWorkingSummaryRow(
            task_id=task.id,
            investigation_run_id=task.investigation_run_id,
            attempt_id=result.attempt_id,
            version=SUMMARY_VERSION,
            summary=summary.model_dump(mode="json"),
            content_sha256=material_hash(summary),
            created_at=result.created_at,
        )
    )


async def validated_summary(
    session: AsyncSession,
    task: AgentTaskRow,
    result: TaskResultRecord,
    row: AgentTaskWorkingSummaryRow,
) -> WorkingSummaryRecord:
    try:
        record = WorkingSummaryRecord.model_validate(row, from_attributes=True)
    except ValueError as error:
        raise TaskFactConflict(
            "working_summary_source_invalid", "工作摘要散列或归属不一致"
        ) from error
    if record.summary != await expected_summary(session, task, result):
        raise TaskFactConflict("working_summary_source_invalid", "工作摘要与持久来源不一致")
    return record
