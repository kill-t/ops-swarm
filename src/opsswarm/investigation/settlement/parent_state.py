"""交接历史使用的窄收尾校验，避免完整来源重建递归。"""

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.execution.release.tables import CandidateReleaseRequestRow
from opsswarm.execution.release_jobs.contracts import ExecutionResultContent
from opsswarm.execution.release_jobs.tables import ReleaseExecutionResultRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.handoff.tables import InvestigationTerminalHandoffRow
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.settlement.contracts import (
    EVENT_TYPE,
    SettlementContent,
    SettlementError,
    SettlementRecord,
)
from opsswarm.investigation.settlement.policy import (
    disposition,
    require_execution_binding,
    settlement_id,
)
from opsswarm.investigation.settlement.tables import CandidateSettlementRow
from opsswarm.investigation.tasks.artifact_tables import AgentTaskEventRow
from opsswarm.verification.candidate.contracts import RecoveryResultContent
from opsswarm.verification.candidate.tables import RecoveryJobRow, RecoveryResultRow


def decode_record(row: CandidateSettlementRow) -> SettlementRecord:
    record = SettlementRecord.model_validate(row, from_attributes=True)
    content = SettlementContent.model_validate(record.model_dump(exclude={"content_sha256"}))
    if record.id != settlement_id(
        record.investigation_run_id
    ) or record.content_sha256 != content_hash(content):
        raise ValueError("收尾身份或内容摘要不一致")
    return record


async def parent_settlement(
    session: AsyncSession,
    run: InvestigationRunRow,
) -> SettlementRecord:
    """只校验直接引用；完整128历史由收尾入口重建，不能在这里递归调用。"""
    try:
        row = await session.scalar(
            select(CandidateSettlementRow).where(
                CandidateSettlementRow.investigation_run_id == run.id
            )
        )
        if row is None:
            raise ValueError("父终态缺少收尾事实")
        record = decode_record(row)
        recovery = await session.get(RecoveryJobRow, record.recovery_job_id)
        recovery_result = await session.get(RecoveryResultRow, record.recovery_job_id)
        execution_result = await session.get(ReleaseExecutionResultRow, record.recovery_job_id)
        handoff = await session.get(InvestigationTerminalHandoffRow, record.handoff_id)
        event = await session.get(AgentTaskEventRow, (run.id, record.event_cursor))
        if (
            recovery is None
            or recovery_result is None
            or execution_result is None
            or handoff is None
        ):
            raise ValueError("收尾直接来源不存在")
        checked_recovery = RecoveryResultContent.model_validate(recovery_result.content)
        checked_execution = ExecutionResultContent.model_validate(execution_result.content)
        require_execution_binding(checked_execution.outcome, checked_recovery)
        request = await session.get(CandidateReleaseRequestRow, checked_execution.request_id)
        event_count = await session.scalar(
            select(func.count())
            .select_from(AgentTaskEventRow)
            .where(
                AgentTaskEventRow.investigation_run_id == run.id,
                AgentTaskEventRow.event_type == EVENT_TYPE,
            )
        )
        if (
            recovery.investigation_run_id != run.id
            or recovery.candidate_job_id != record.candidate_job_id
            or recovery.verification_version != record.verification_version
            or recovery.operation_id != record.operation_id
            or recovery.status != "completed"
            or record.execution_result_sha256 != recovery.execution_result_sha256
            or record.execution_result_sha256 != execution_result.content_sha256
            or record.execution_result_sha256 != raw_content_hash(execution_result.content)
            or record.execution_result_sha256 != content_hash(checked_execution)
            or checked_execution.operation_id != record.operation_id
            or record.recovery_result_sha256 != recovery_result.content_sha256
            or record.recovery_result_sha256 != raw_content_hash(recovery_result.content)
            or record.recovery_result_sha256 != content_hash(checked_recovery)
            or checked_recovery.execution_job_id != record.recovery_job_id
            or checked_recovery.execution_result_sha256 != record.execution_result_sha256
            or checked_recovery.operation_id != record.operation_id
            or checked_recovery.outcome != record.outcome
            or checked_recovery.reason_code != record.reason_code
            or disposition(checked_recovery) != record.disposition
            or recovery.completed_at != recovery_result.created_at
            or recovery_result.created_at != checked_recovery.created_at
            or request is None
            or request.incident_version != record.incident_version
            or request.investigation_run_id != run.id
            or request.candidate_job_id != record.candidate_job_id
            or recovery_result.created_at > record.created_at
            or record.previous_run_updated_at < handoff.created_at
            or record.previous_run_updated_at > record.created_at
            or handoff.investigation_run_id != run.id
            or record.previous_run_status
            != ("await_human" if handoff.destination == "await_human" else "await_candidate")
            or event is None
            or event.event_type != EVENT_TYPE
            or event.task_id is not None
            or event.occurred_at != record.created_at
            or event_count != 1
            or run.status != "settled"
            or run.completed_at != record.created_at
            or run.updated_at != record.created_at
            or run.result is not None
            or run.metrics is not None
            or run.last_error is not None
        ):
            raise ValueError("收尾与父状态、直接结果或事件不一致")
        return record
    except (ValueError, TypeError) as error:
        raise SettlementError(
            "candidate_settlement_integrity_violation", "父运行收尾来源校验失败"
        ) from error
