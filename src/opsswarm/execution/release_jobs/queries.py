"""执行任务、连续尝试、原始审计与不可变结果的完整历史重建。"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.execution.release.contracts import ReleaseRequestRecord
from opsswarm.execution.release.tables import CandidateReleaseRequestRow
from opsswarm.execution.release_jobs.authorization import validate_admission
from opsswarm.execution.release_jobs.contracts import (
    POLICY_VERSION,
    ExecutionAttemptRecord,
    ExecutionAuditRecord,
    ExecutionCommand,
    ExecutionJobError,
    ExecutionJobRecord,
    ExecutionJobView,
    ExecutionResultContent,
)
from opsswarm.execution.release_jobs.policy import (
    dispatch_command,
    execution_id,
    result_content,
    terminal_status,
)
from opsswarm.execution.release_jobs.tables import (
    ReleaseExecutionAttemptRow,
    ReleaseExecutionCommandRow,
    ReleaseExecutionJobRow,
    ReleaseExecutionResultRow,
)
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.context.hashing import content_hash


async def read_audit(
    session: AsyncSession, row: ReleaseExecutionCommandRow
) -> ExecutionAuditRecord:
    checked = ExecutionAuditRecord.model_validate(row, from_attributes=True)
    admission = await session.get(OperatorAuthorizationRow, row.authorization_id)
    validate_admission(
        admission,
        run_id=row.investigation_run_id,
        job_id=row.candidate_job_id,
        version=row.verification_version,
        subject_id=row.subject_id,
        command=checked.command,
        raw_body=row.raw_body,
        occurred_at=row.created_at,
    )
    if row.command != checked.command.model_dump(mode="json") or row.command_sha256 != content_hash(
        checked.command
    ):
        raise ExecutionJobError("release_execution_integrity_violation", "执行命令审计摘要不一致")
    if checked.execution_job_id is not None:
        job = await session.get(ReleaseExecutionJobRow, checked.execution_job_id)
        request = (
            None if job is None else await session.get(CandidateReleaseRequestRow, job.request_id)
        )
        if (
            job is None
            or request is None
            or checked.command.expected_incident_version != request.incident_version
            or not matches(job, row.candidate_job_id, row.verification_version, checked.command)
            or job.investigation_run_id != row.investigation_run_id
            or job.requested_by_id != row.subject_id
            or row.created_at < job.created_at
        ):
            raise ExecutionJobError("release_execution_integrity_violation", "执行审计缺少精确任务")
        if checked.outcome == "applied" and (
            row.authorization_id != job.authorization_id or row.created_at != job.created_at
        ):
            raise ExecutionJobError(
                "release_execution_integrity_violation", "执行创建审计与原任务不一致"
            )
        if checked.outcome == "replayed" and row.authorization_id == job.authorization_id:
            raise ExecutionJobError(
                "release_execution_integrity_violation", "执行重放不能使用原创建授权"
            )
    return checked


def matches(
    row: ReleaseExecutionJobRow, job_id: UUID, version: int, command: ExecutionCommand
) -> bool:
    return (
        row.candidate_job_id == job_id
        and row.verification_version == version
        and row.request_id == command.request_id
        and row.request_content_sha256 == command.request_content_sha256
        and row.idempotency_key == command.idempotency_key
        and row.command_sha256 == content_hash(command)
    )


async def read_view(
    session: AsyncSession, row: ReleaseExecutionJobRow, request: ReleaseRequestRecord
) -> ExecutionJobView:
    try:
        record = ExecutionJobRecord.model_validate(row, from_attributes=True)
        expected = dispatch_command(
            request,
            created_at=record.created_at,
            credential_expires_at=record.credential_expires_at,
        )
        if (
            record.id != execution_id(request.id)
            or record.policy_version != POLICY_VERSION
            or record.request_id != request.id
            or record.request_content_sha256 != request.content_sha256
            or record.operation_id != request.operation_id
            or record.investigation_run_id != request.investigation_run_id
            or record.candidate_job_id != request.candidate_job_id
            or record.verification_version != request.verification_version
            or record.created_at < request.created_at
            or record.updated_at < record.created_at
            or record.dispatch_command != expected
            or record.dispatch_sha256 != content_hash(expected)
            or record.dispatch_sha256 != raw_content_hash(row.dispatch_command)
        ):
            raise ValueError("执行任务未绑定完整原请求和固定窗口")
        creator = await session.scalar(
            select(ReleaseExecutionCommandRow).where(
                ReleaseExecutionCommandRow.authorization_id == record.authorization_id
            )
        )
        if creator is None or creator.outcome != "applied" or creator.execution_job_id != record.id:
            raise ValueError("执行任务缺少同源成功创建审计")
        await read_audit(session, creator)
        admission = await session.get(OperatorAuthorizationRow, record.authorization_id)
        if admission is None or admission.credential_id != record.credential_id:
            raise ValueError("执行委派缺少同源凭据")
        attempts = tuple(
            ExecutionAttemptRecord.model_validate(a, from_attributes=True)
            for a in await session.scalars(
                select(ReleaseExecutionAttemptRow)
                .where(ReleaseExecutionAttemptRow.job_id == row.id)
                .order_by(ReleaseExecutionAttemptRow.number)
            )
        )
        if (
            len(attempts) != record.attempt_count
            or tuple(a.number for a in attempts) != tuple(range(1, record.attempt_count + 1))
            or any(
                a.started_at < record.created_at
                or (a.finished_at is not None and a.finished_at < a.started_at)
                for a in attempts
            )
        ):
            raise ValueError("执行尝试历史不连续")
        if any(
            a.status != "lease_expired" or a.finished_at != b.started_at
            for a, b in zip(attempts, attempts[1:], strict=False)
        ):
            raise ValueError("执行接管缺少原过期尝试")
        if record.status == "running" and (
            not attempts
            or attempts[-1].status != "running"
            or record.lease_token != attempts[-1].lease_token
            or record.locked_until != attempts[-1].lease_expires_at
            or record.locked_by != attempts[-1].worker_id
        ):
            raise ValueError("执行租约与当前尝试不一致")
        if record.phase == "dispatched":
            origin = next((a for a in attempts if a.id == record.dispatch_attempt_id), None)
            if (
                origin is None
                or record.dispatched_at is None
                or not origin.started_at <= record.dispatched_at < origin.lease_expires_at
                or not expected.not_before <= record.dispatched_at < expected.expires_at
                or expected.expires_at > origin.lease_expires_at
            ):
                raise ValueError("执行派发缺少固定窗口内首次尝试")
        stored = await session.get(ReleaseExecutionResultRow, row.id)
        content = None
        if record.status in {"succeeded", "failed", "unknown"}:
            if stored is None or not attempts or stored.attempt_id != attempts[-1].id:
                raise ValueError("执行终态缺少原子结果")
            content = ExecutionResultContent.model_validate(stored.content)
            rebuilt = result_content(
                record, content.observation, now=stored.created_at, reason_code=content.reason_code
            )
            if (
                content != rebuilt
                or stored.content_sha256 != content_hash(content)
                or stored.content_sha256 != raw_content_hash(stored.content)
                or content.created_at != stored.created_at
                or record.completed_at != stored.created_at
                or record.updated_at != stored.created_at
                or terminal_status(content) != record.status
                or attempts[-1].finished_at != stored.created_at
            ):
                raise ValueError("执行结果与终态不一致")
            last = attempts[-1]
            if last.status == "lease_expired":
                if (
                    last.number != 3
                    or stored.created_at < last.lease_expires_at
                    or content.observation is not None
                ):
                    raise ValueError("执行耗尽未保留未知边界")
            elif (
                last.status != ("succeeded" if record.status == "succeeded" else "failed")
                or not last.started_at <= stored.created_at < last.lease_expires_at
            ):
                raise ValueError("执行结果没有有效结算尝试")
        elif stored is not None or (record.status == "queued" and attempts):
            raise ValueError("未结算执行不能已有结果")
        return ExecutionJobView(record=record, attempts=attempts, result=content)
    except (ValueError, TypeError) as error:
        raise ExecutionJobError(
            "release_execution_integrity_violation", "执行任务历史无法完整重建"
        ) from error
