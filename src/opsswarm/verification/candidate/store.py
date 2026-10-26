"""独立恢复验证的发现、租约、逐样本保存与原子结算。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.execution.release.observer import utc_now
from opsswarm.execution.release_jobs.contracts import ExecutionJobView
from opsswarm.execution.release_jobs.store import lock_parent
from opsswarm.execution.release_jobs.tables import ReleaseExecutionJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import WorkItemRow
from opsswarm.verification.candidate.contracts import (
    ProbeObservation,
    RecoveryClaim,
    RecoveryError,
    RecoveryJobRecord,
    RecoverySampleRecord,
    RecoveryView,
)
from opsswarm.verification.candidate.policy import POLICY_VERSION, validate_observation
from opsswarm.verification.candidate.queries import make_result, recovery_view
from opsswarm.verification.candidate.source import execution_source, fixed_window
from opsswarm.verification.candidate.tables import (
    RecoveryAttemptRow,
    RecoveryJobRow,
    RecoveryResultRow,
    RecoverySampleRow,
)


class CandidateRecoveryStore:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._factory, self._clock = factory, clock

    def _now(self) -> datetime:
        return normalized_now(self._clock)

    async def get(self, run_id: UUID, candidate_id: UUID, version: int) -> RecoveryView:
        async with self._factory() as session, session.begin():
            run = await lock_parent(session, run_id, candidate_id, read=True)
            execution = await session.scalar(
                select(ReleaseExecutionJobRow).where(
                    ReleaseExecutionJobRow.investigation_run_id == run_id,
                    ReleaseExecutionJobRow.candidate_job_id == candidate_id,
                    ReleaseExecutionJobRow.verification_version == version,
                )
            )
            if execution is None:
                return _empty()
            source = await execution_source(session, run, execution.id, clock=self._clock)
            row = await session.get(RecoveryJobRow, execution.id)
            if row is None:
                return _empty()
            return await recovery_view(session, row, run, source)

    async def claim_next(self, *, worker_id: str) -> RecoveryClaim | None:
        if not worker_id.strip() or len(worker_id) > 100:
            raise ValueError("恢复验证Worker标识无效")
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                now = self._now()
                candidate = (
                    await session.execute(
                        select(ReleaseExecutionJobRow)
                        .outerjoin(RecoveryJobRow, RecoveryJobRow.id == ReleaseExecutionJobRow.id)
                        .where(
                            ReleaseExecutionJobRow.id.not_in(skipped),
                            ReleaseExecutionJobRow.status.in_(("succeeded", "failed", "unknown")),
                            or_(
                                RecoveryJobRow.id.is_(None),
                                RecoveryJobRow.status == "queued",
                                and_(
                                    RecoveryJobRow.status == "running",
                                    or_(
                                        RecoveryJobRow.locked_until <= now,
                                        RecoveryJobRow.window_end <= now,
                                    ),
                                ),
                            ),
                        )
                        .order_by(ReleaseExecutionJobRow.completed_at, ReleaseExecutionJobRow.id)
                        .limit(1)
                    )
                ).scalar_one_or_none()
                if candidate is None:
                    return None
                skipped.append(candidate.id)
                work = await session.scalar(
                    select(WorkItemRow)
                    .where(WorkItemRow.investigation_run_id == candidate.investigation_run_id)
                    .with_for_update(skip_locked=True)
                )
                if work is None:
                    continue
                run = await lock_parent(
                    session, candidate.investigation_run_id, candidate.candidate_job_id
                )
                source = await execution_source(session, run, candidate.id, clock=self._clock)
                assert source.result is not None
                row = await session.scalar(
                    select(RecoveryJobRow)
                    .where(RecoveryJobRow.id == candidate.id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                now = self._now()
                if row is None:
                    start, end = fixed_window(run, source)
                    row = RecoveryJobRow(
                        id=candidate.id,
                        investigation_run_id=run.id,
                        candidate_job_id=candidate.candidate_job_id,
                        verification_version=candidate.verification_version,
                        operation_id=source.result.operation_id,
                        execution_result_sha256=content_hash(source.result),
                        policy_version=POLICY_VERSION,
                        window_start=start,
                        window_end=end,
                        status="queued",
                        attempt_count=0,
                        locked_by=None,
                        lease_token=None,
                        locked_until=None,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                    )
                    session.add(row)
                    await session.flush()
                view = await recovery_view(session, row, run, source)
                assert view.record is not None
                if row.status == "completed" or (
                    row.status == "running"
                    and row.locked_until is not None
                    and row.locked_until > now
                    and row.window_end > now
                ):
                    continue
                if row.status == "running":
                    previous = await session.get(RecoveryAttemptRow, view.attempts[-1].id)
                    assert previous is not None
                    previous.status, previous.finished_at = "lease_expired", now
                    await session.flush()
                if (
                    source.result.outcome != "applied_current"
                    or row.window_end <= now
                    or row.attempt_count == 3
                ):
                    await self._finish(
                        session,
                        row,
                        view.record,
                        source,
                        tuple(
                            s
                            for s in view.samples
                            if view.attempts and s.attempt_id == view.attempts[-1].id
                        ),
                        view.attempts[-1].id if view.attempts else None,
                        now=now,
                        exhausted=source.result.outcome == "applied_current",
                    )
                    continue
                attempt_id, token = uuid4(), uuid4()
                row.status, row.attempt_count, row.locked_by = (
                    "running",
                    row.attempt_count + 1,
                    worker_id,
                )
                row.lease_token, row.locked_until, row.updated_at = (
                    token,
                    now + timedelta(seconds=30),
                    now,
                )
                await session.flush()
                session.add(
                    RecoveryAttemptRow(
                        id=attempt_id,
                        job_id=row.id,
                        number=row.attempt_count,
                        lease_token=token,
                        worker_id=worker_id,
                        status="running",
                        started_at=now,
                        lease_expires_at=row.locked_until,
                        finished_at=None,
                    )
                )
                await session.flush()
                claim = RecoveryClaim(
                    job_id=row.id,
                    run_id=run.id,
                    attempt_id=attempt_id,
                    number=row.attempt_count,
                    lease_token=token,
                    lease_expires_at=row.locked_until,
                )
                _require_lease(row, claim, self._now())
                return claim
        return None

    async def _active(
        self, session: AsyncSession, claim: RecoveryClaim
    ) -> tuple[
        RecoveryJobRow,
        RecoveryView,
        ExecutionJobView,
    ]:
        original = await session.get(RecoveryJobRow, claim.job_id)
        if original is None or original.investigation_run_id != claim.run_id:
            raise RecoveryError("recovery_lease_lost", "恢复验证租约不存在")
        run = await lock_parent(session, claim.run_id, original.candidate_job_id)
        row = await session.scalar(
            select(RecoveryJobRow)
            .where(RecoveryJobRow.id == claim.job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert row is not None
        _require_lease(row, claim, self._now())
        source = await execution_source(session, run, row.id, clock=self._clock)
        view = await recovery_view(session, row, run, source)
        if not view.attempts or view.attempts[-1].id != claim.attempt_id:
            raise RecoveryError("recovery_lease_lost", "恢复验证尝试已被接管")
        _require_lease(row, claim, self._now())
        return row, view, source

    async def authorize_sample(self, claim: RecoveryClaim) -> int:
        async with self._factory() as session, session.begin():
            row, view, _ = await self._active(session, claim)
            samples = tuple(s for s in view.samples if s.attempt_id == claim.attempt_id)
            if len(samples) >= 3 or any(s.error_code is not None for s in samples):
                raise RecoveryError("recovery_samples_complete", "本尝试已完成采样")
            if samples and self._now() < samples[-1].created_at + timedelta(seconds=2):
                raise RecoveryError("recovery_sample_too_early", "恢复采样间隔不足")
            _require_lease(row, claim, self._now())
            return len(samples) + 1

    async def save_sample(
        self,
        claim: RecoveryClaim,
        sequence: int,
        *,
        observation: ProbeObservation | None = None,
        error_code: str | None = None,
    ) -> RecoverySampleRecord:
        async with self._factory() as session, session.begin():
            row, view, _ = await self._active(session, claim)
            now = self._now()
            samples = tuple(s for s in view.samples if s.attempt_id == claim.attempt_id)
            if sequence != len(samples) + 1 or sequence > 3 or any(s.error_code for s in samples):
                raise RecoveryError("recovery_sample_conflict", "恢复样本序号已保存或无效")
            if observation is not None:
                observation = validate_observation(observation, now)
                if observation.requested_at < view.attempts[-1].started_at or (
                    samples
                    and observation.requested_at < samples[-1].created_at + timedelta(seconds=2)
                ):
                    raise RecoveryError("recovery_sample_conflict", "恢复样本请求时间无效")
            elif samples and now < samples[-1].created_at + timedelta(seconds=2):
                raise RecoveryError("recovery_sample_too_early", "恢复采样间隔不足")
            sample = RecoverySampleRecord(
                job_id=row.id,
                attempt_id=claim.attempt_id,
                sequence=sequence,
                observation=observation,
                error_code=error_code,
                created_at=now,
            )
            session.add(
                RecoverySampleRow(
                    job_id=row.id,
                    attempt_id=claim.attempt_id,
                    sequence=sequence,
                    observation=None
                    if observation is None
                    else observation.model_dump(mode="json"),
                    error_code=error_code,
                    created_at=now,
                )
            )
            await session.flush()
            _require_lease(row, claim, self._now())
            return sample

    async def complete(self, claim: RecoveryClaim) -> None:
        async with self._factory() as session, session.begin():
            row, view, source = await self._active(session, claim)
            assert view.record is not None
            samples = tuple(s for s in view.samples if s.attempt_id == claim.attempt_id)
            if len(samples) != 3 and not any(s.error_code for s in samples):
                raise RecoveryError("recovery_samples_incomplete", "恢复样本尚未完成")
            now = self._now()
            result = make_result(view.record, source, samples, attempt_id=claim.attempt_id, now=now)
            attempt = await session.get(RecoveryAttemptRow, claim.attempt_id)
            assert attempt is not None
            attempt.status = "succeeded" if result.outcome == "recovered" else "failed"
            attempt.finished_at = now
            await session.flush()
            await self._finish(
                session, row, view.record, source, samples, claim.attempt_id, now=now
            )
            if self._now() >= min(claim.lease_expires_at, row.window_end):
                raise RecoveryError("recovery_lease_lost", "恢复结算提交前窗口或租约过期")

    async def _finish(
        self,
        session: AsyncSession,
        row: RecoveryJobRow,
        record: RecoveryJobRecord,
        source: ExecutionJobView,
        samples: tuple[RecoverySampleRecord, ...],
        attempt_id: UUID | None,
        *,
        now: datetime,
        exhausted: bool = False,
    ) -> None:
        result = make_result(
            record, source, samples, attempt_id=attempt_id, now=now, exhausted=exhausted
        )
        row.status, row.completed_at, row.updated_at = "completed", now, now
        row.locked_by, row.lease_token, row.locked_until = None, None, None
        await session.flush()
        session.add(
            RecoveryResultRow(
                job_id=row.id,
                content=result.model_dump(mode="json"),
                content_sha256=content_hash(result),
                created_at=now,
            )
        )
        await session.flush()


def _require_lease(row: RecoveryJobRow, claim: RecoveryClaim, now: datetime) -> None:
    if (
        row.status != "running"
        or row.lease_token != claim.lease_token
        or row.locked_until != claim.lease_expires_at
        or row.attempt_count != claim.number
        or not row.window_start <= now < min(claim.lease_expires_at, row.window_end)
    ):
        raise RecoveryError("recovery_lease_lost", "恢复验证窗口或租约已失效")


def _empty() -> RecoveryView:
    return RecoveryView(record=None, attempts=(), samples=(), result=None)
