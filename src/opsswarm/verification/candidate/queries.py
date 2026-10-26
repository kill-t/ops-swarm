"""逐次样本与独立尝试的历史重建及确定性恢复判定。"""

from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.execution.release_jobs.contracts import ExecutionJobView
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.verification.candidate.contracts import (
    RecoveryAttemptRecord,
    RecoveryError,
    RecoveryJobRecord,
    RecoveryResultContent,
    RecoverySampleRecord,
    RecoveryView,
)
from opsswarm.verification.candidate.policy import POLICY_VERSION, classify, validate_observation
from opsswarm.verification.candidate.source import fixed_window
from opsswarm.verification.candidate.tables import (
    RecoveryAttemptRow,
    RecoveryJobRow,
    RecoveryResultRow,
    RecoverySampleRow,
)


def make_result(
    record: RecoveryJobRecord,
    source: ExecutionJobView,
    samples: tuple[RecoverySampleRecord, ...],
    *,
    attempt_id: UUID | None,
    now: datetime,
    exhausted: bool = False,
) -> RecoveryResultContent:
    assert source.result is not None
    outcome, reason = classify(source.result.outcome, samples, exhausted=exhausted)
    return RecoveryResultContent(
        execution_job_id=record.id,
        execution_result_sha256=record.execution_result_sha256,
        operation_id=record.operation_id,
        outcome=outcome,
        attempt_id=attempt_id,
        reason_code=reason,
        created_at=now,
        parent_advanced=False,
    )


async def recovery_view(
    session: AsyncSession,
    row: RecoveryJobRow,
    run: InvestigationRunRow,
    source: ExecutionJobView,
) -> RecoveryView:
    try:
        assert source.record is not None and source.result is not None
        record = RecoveryJobRecord.model_validate(row, from_attributes=True)
        if (
            record.id != source.record.id
            or record.investigation_run_id != run.id
            or record.candidate_job_id != source.record.candidate_job_id
            or record.verification_version != source.record.verification_version
            or record.operation_id != source.result.operation_id
            or record.execution_result_sha256 != content_hash(source.result)
            or record.policy_version != POLICY_VERSION
            or (record.window_start, record.window_end) != fixed_window(run, source)
            or record.created_at < record.window_start
            or record.updated_at < record.created_at
        ):
            raise ValueError("恢复任务未绑定精确执行结果与原窗口")
        attempts = tuple(
            RecoveryAttemptRecord.model_validate(a, from_attributes=True)
            for a in await session.scalars(
                select(RecoveryAttemptRow)
                .where(RecoveryAttemptRow.job_id == row.id)
                .order_by(RecoveryAttemptRow.number)
            )
        )
        if len(attempts) != record.attempt_count or tuple(a.number for a in attempts) != tuple(
            range(1, record.attempt_count + 1)
        ):
            raise ValueError("恢复尝试不连续")
        if source.result.outcome != "applied_current" and attempts:
            raise ValueError("非当前应用结果不能创建探测尝试")
        if record.status != "running" and any(
            value is not None
            for value in (record.locked_by, record.lease_token, record.locked_until)
        ):
            raise ValueError("非运行任务不能保留租约")
        for a in attempts:
            if (
                a.started_at < record.created_at
                or a.job_id != record.id
                or a.lease_expires_at != a.started_at + timedelta(seconds=30)
                or (a.finished_at is not None and a.finished_at < a.started_at)
                or (
                    a.status == "lease_expired"
                    and (
                        a.finished_at is None
                        or a.finished_at < min(a.lease_expires_at, record.window_end)
                    )
                )
            ):
                raise ValueError("恢复尝试时间无效")
        if any(
            a.status != "lease_expired"
            or a.finished_at != b.started_at
            or b.started_at < a.lease_expires_at
            for a, b in zip(attempts, attempts[1:], strict=False)
        ):
            raise ValueError("恢复接管缺少过期前驱")
        sample_rows = tuple(
            await session.scalars(
                select(RecoverySampleRow)
                .join(RecoveryAttemptRow, RecoveryAttemptRow.id == RecoverySampleRow.attempt_id)
                .where(RecoverySampleRow.job_id == row.id)
                .order_by(RecoveryAttemptRow.number, RecoverySampleRow.sequence)
            )
        )
        samples = tuple(
            RecoverySampleRecord.model_validate(s, from_attributes=True) for s in sample_rows
        )
        if any(
            raw.observation
            != (None if parsed.observation is None else parsed.observation.model_dump(mode="json"))
            for raw, parsed in zip(sample_rows, samples, strict=True)
        ):
            raise ValueError("样本原文不能由默认字段补齐")
        for a in attempts:
            group = tuple(s for s in samples if s.attempt_id == a.id)
            if tuple(s.sequence for s in group) != tuple(range(1, len(group) + 1)):
                raise ValueError("样本序号不连续")
            previous = None
            for s in group:
                if s.job_id != row.id or not a.started_at <= s.created_at < min(
                    a.lease_expires_at, record.window_end
                ):
                    raise ValueError("样本不在原窗口与租约内")
                if previous is not None and s.created_at < previous + timedelta(seconds=2):
                    raise ValueError("样本保存间隔不足")
                if s.observation is not None:
                    obs = validate_observation(s.observation, s.created_at)
                    if not a.started_at <= obs.requested_at or (
                        previous is not None and obs.requested_at < previous + timedelta(seconds=2)
                    ):
                        raise ValueError("样本来源或间隔无效")
                previous = s.created_at
        if any(s.attempt_id not in {a.id for a in attempts} for s in samples):
            raise ValueError("样本没有所属尝试")
        if record.status == "running" and (
            not attempts
            or attempts[-1].status != "running"
            or record.locked_until != attempts[-1].lease_expires_at
            or record.lease_token != attempts[-1].lease_token
            or record.locked_by != attempts[-1].worker_id
        ):
            raise ValueError("恢复租约与尝试不一致")
        stored = await session.get(RecoveryResultRow, row.id)
        result = None
        if record.status == "completed":
            if stored is None:
                raise ValueError("恢复终态缺少结果")
            result = RecoveryResultContent.model_validate(stored.content)
            last = attempts[-1] if attempts else None
            exhausted = source.result.outcome == "applied_current" and (
                last is None or last.status == "lease_expired"
            )
            chosen = tuple(s for s in samples if last is not None and s.attempt_id == last.id)
            rebuilt = make_result(
                record,
                source,
                chosen,
                attempt_id=last.id if last else None,
                now=stored.created_at,
                exhausted=exhausted,
            )
            if (
                result != rebuilt
                or stored.content_sha256 != content_hash(result)
                or stored.content_sha256 != raw_content_hash(stored.content)
                or record.completed_at != stored.created_at
                or record.updated_at != stored.created_at
                or (last is not None and last.finished_at != stored.created_at)
            ):
                raise ValueError("恢复结果与样本终态不一致")
            if exhausted:
                if record.attempt_count < 3 and stored.created_at < record.window_end:
                    raise ValueError("窗口和尝试均未耗尽")
            elif source.result.outcome == "applied_current" and (
                last is None
                or last.status != ("succeeded" if result.outcome == "recovered" else "failed")
                or not last.started_at
                <= stored.created_at
                < min(last.lease_expires_at, record.window_end)
            ):
                raise ValueError("恢复结算缺少有效租约")
        elif stored is not None or record.completed_at is not None:
            raise ValueError("未完成任务不能有结果")
        if record.status == "queued" and attempts:
            raise ValueError("待领取任务不能有尝试")
        return RecoveryView(record=record, attempts=attempts, samples=samples, result=result)
    except (ValueError, TypeError) as error:
        raise RecoveryError("recovery_integrity_violation", "恢复验证历史无法完整重建") from error
