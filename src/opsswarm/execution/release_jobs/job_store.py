"""执行任务的独立租约、永久派发标记与只读结果原子结算。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.execution.release.contracts import ReleaseRequestRecord
from opsswarm.execution.release.observer import utc_now
from opsswarm.execution.release.queries import ReleaseContext
from opsswarm.execution.release_jobs.authorization import credential_for
from opsswarm.execution.release_jobs.client import DispatchObservation
from opsswarm.execution.release_jobs.contracts import (
    ExecutionClaim,
    ExecutionJobError,
    ExecutionJobRecord,
)
from opsswarm.execution.release_jobs.policy import require_window, result_content, terminal_status
from opsswarm.execution.release_jobs.queries import read_view
from opsswarm.execution.release_jobs.source import load_request, require_current
from opsswarm.execution.release_jobs.store import lock_parent
from opsswarm.execution.release_jobs.tables import (
    ReleaseExecutionAttemptRow,
    ReleaseExecutionJobRow,
    ReleaseExecutionResultRow,
)
from opsswarm.fault_lab.release.dispatch_contracts import DispatchCommand
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow


class ReleaseExecutionJobStore:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        auth_provider: Callable[[], OperatorAuthSettings] = OperatorAuthSettings,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._factory, self._auth_provider, self._clock = factory, auth_provider, clock

    def _now(self) -> datetime:
        return normalized_now(self._clock)

    async def claim_next(self, *, worker_id: str) -> ExecutionClaim | None:
        if not worker_id.strip() or len(worker_id) > 100:
            raise ValueError("执行Worker标识无效")
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                now = self._now()
                candidate = (
                    await session.execute(
                        select(
                            ReleaseExecutionJobRow.id, ReleaseExecutionJobRow.investigation_run_id
                        )
                        .where(
                            ReleaseExecutionJobRow.id.not_in(skipped),
                            or_(
                                ReleaseExecutionJobRow.status == "queued",
                                and_(
                                    ReleaseExecutionJobRow.status == "running",
                                    ReleaseExecutionJobRow.locked_until <= now,
                                ),
                            ),
                        )
                        .order_by(ReleaseExecutionJobRow.created_at, ReleaseExecutionJobRow.id)
                        .limit(1)
                    )
                ).one_or_none()
                if candidate is None:
                    return None
                job_id, run_id = candidate
                skipped.append(job_id)
                work = await session.scalar(
                    select(WorkItemRow)
                    .where(WorkItemRow.investigation_run_id == run_id)
                    .with_for_update(skip_locked=True)
                )
                if work is None:
                    continue
                row = await session.get(ReleaseExecutionJobRow, job_id)
                assert row is not None
                run = await lock_parent(session, run_id, row.candidate_job_id)
                row = await session.scalar(
                    select(ReleaseExecutionJobRow)
                    .where(ReleaseExecutionJobRow.id == job_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                assert row is not None
                now = self._now()
                if row.status != "queued" and not (
                    row.status == "running"
                    and row.locked_until is not None
                    and row.locked_until <= now
                ):
                    continue
                _, request = await load_request(
                    session, run, row.candidate_job_id, row.verification_version, clock=self._clock
                )
                if request is None:
                    raise ExecutionJobError(
                        "release_execution_integrity_violation", "执行任务缺少准备事实"
                    )
                view = await read_view(session, row, request)
                assert view.record is not None
                if row.status == "running":
                    previous = await session.get(ReleaseExecutionAttemptRow, view.attempts[-1].id)
                    assert previous is not None
                    previous.status, previous.finished_at, previous.error_code = (
                        "lease_expired",
                        now,
                        "release_execution_lease_lost",
                    )
                    await session.flush()
                if row.attempt_count == 3:
                    content = result_content(
                        view.record,
                        None,
                        now=now,
                        reason_code="release_execution_attempts_exhausted",
                    )
                    _finish(row, terminal_status(content), now, content.reason_code)
                    await session.flush()
                    session.add(
                        ReleaseExecutionResultRow(
                            job_id=row.id,
                            attempt_id=view.attempts[-1].id,
                            content=content.model_dump(mode="json"),
                            content_sha256=content_hash(content),
                            created_at=now,
                        )
                    )
                    await session.flush()
                    continue
                attempt_id, token = uuid4(), uuid4()
                row.attempt_count += 1
                row.status, row.locked_by, row.lease_token = "running", worker_id, token
                row.locked_until, row.updated_at = now + timedelta(seconds=60), now
                await session.flush()
                session.add(
                    ReleaseExecutionAttemptRow(
                        id=attempt_id,
                        job_id=row.id,
                        number=row.attempt_count,
                        lease_token=token,
                        worker_id=worker_id,
                        status="running",
                        started_at=now,
                        lease_expires_at=row.locked_until,
                        finished_at=None,
                        error_code=None,
                    )
                )
                await session.flush()
                claim = ExecutionClaim(
                    job_id=row.id,
                    investigation_run_id=run_id,
                    attempt_id=attempt_id,
                    number=row.attempt_count,
                    lease_token=token,
                    lease_expires_at=row.locked_until,
                )
                _require_lease(row, claim, self._now())
                return claim
        return None

    async def _active(
        self, session: AsyncSession, claim: ExecutionClaim
    ) -> tuple[
        ReleaseExecutionJobRow,
        ExecutionJobRecord,
        InvestigationRunRow,
        ReleaseContext,
        ReleaseRequestRecord,
    ]:
        original = await session.get(ReleaseExecutionJobRow, claim.job_id)
        if original is None or original.investigation_run_id != claim.investigation_run_id:
            raise ExecutionJobError("release_execution_lease_lost", "执行任务或租约不存在")
        run = await lock_parent(session, claim.investigation_run_id, original.candidate_job_id)
        row = await session.scalar(
            select(ReleaseExecutionJobRow)
            .where(ReleaseExecutionJobRow.id == claim.job_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        assert row is not None
        _require_lease(row, claim, self._now())
        context, request = await load_request(
            session, run, row.candidate_job_id, row.verification_version, clock=self._clock
        )
        if request is None:
            raise ExecutionJobError(
                "release_execution_integrity_violation", "执行任务缺少原准备事实"
            )
        view = await read_view(session, row, request)
        if (
            not view.attempts
            or view.attempts[-1].id != claim.attempt_id
            or view.attempts[-1].number != claim.number
        ):
            raise ExecutionJobError("release_execution_lease_lost", "执行尝试不是当前领取身份")
        _require_lease(row, claim, self._now())
        assert view.record is not None
        return row, view.record, run, context, request

    async def get_dispatch(self, claim: ExecutionClaim) -> tuple[DispatchCommand, bool]:
        async with self._factory() as session, session.begin():
            row, record, _, _, _ = await self._active(session, claim)
            return record.dispatch_command, row.phase == "dispatched"

    async def _write_check(
        self,
        session: AsyncSession,
        row: ReleaseExecutionJobRow,
        record: ExecutionJobRecord,
        run: InvestigationRunRow,
        context: ReleaseContext,
        request: ReleaseRequestRecord,
        claim: ExecutionClaim,
    ) -> datetime:
        await require_current(session, run, context, request, clock=self._clock)
        now = self._now()
        credential_for(
            self._auth_provider(),
            credential_id=record.credential_id,
            subject_id=record.requested_by_id,
            now=now,
            expected_binding=record.credential_binding_sha256,
        )
        _require_lease(row, claim, now)
        require_window(record, now, claim.lease_expires_at)
        return now

    async def begin_dispatch(self, claim: ExecutionClaim) -> DispatchCommand:
        async with self._factory() as session, session.begin():
            row, record, run, context, request = await self._active(session, claim)
            if row.phase != "prepared":
                raise ExecutionJobError(
                    "release_execution_already_dispatched", "派发标记已提交，只允许GET恢复"
                )
            now = await self._write_check(session, row, record, run, context, request, claim)
            row.phase, row.dispatched_at, row.dispatch_attempt_id, row.updated_at = (
                "dispatched",
                now,
                claim.attempt_id,
                now,
            )
            await session.flush()
            await self._write_check(session, row, record, run, context, request, claim)
            return record.dispatch_command

    async def authorize_execute(self, claim: ExecutionClaim) -> DispatchCommand:
        # POST返回后必须是新会话/事务，重新加载最高版本、预算和当前配置。
        async with self._factory() as session, session.begin():
            row, record, run, context, request = await self._active(session, claim)
            if row.phase != "dispatched" or row.dispatch_attempt_id != claim.attempt_id:
                raise ExecutionJobError(
                    "release_execution_read_only_recovery", "恢复尝试不能再次写出"
                )
            await self._write_check(session, row, record, run, context, request, claim)
            return record.dispatch_command

    async def complete(
        self,
        claim: ExecutionClaim,
        observation: DispatchObservation | None = None,
        *,
        reason_code: str,
    ) -> None:
        async with self._factory() as session, session.begin():
            row, record, _, _, _ = await self._active(session, claim)
            now = self._now()
            content = result_content(record, observation, now=now, reason_code=reason_code)
            attempt = await session.get(ReleaseExecutionAttemptRow, claim.attempt_id)
            assert attempt is not None
            session.add(
                ReleaseExecutionResultRow(
                    job_id=row.id,
                    attempt_id=attempt.id,
                    content=content.model_dump(mode="json"),
                    content_sha256=content_hash(content),
                    created_at=now,
                )
            )
            await session.flush()
            status = terminal_status(content)
            attempt.status, attempt.finished_at, attempt.error_code = (
                "succeeded" if status == "succeeded" else "failed",
                now,
                None if status == "succeeded" else reason_code,
            )
            _finish(row, status, now, None if status == "succeeded" else reason_code)
            await session.flush()
            if self._now() >= claim.lease_expires_at:
                raise ExecutionJobError("release_execution_lease_lost", "执行结果提交前租约已失效")


def _require_lease(row: ReleaseExecutionJobRow, claim: ExecutionClaim, now: datetime) -> None:
    if (
        row.status != "running"
        or row.lease_token != claim.lease_token
        or row.locked_until != claim.lease_expires_at
        or row.attempt_count != claim.number
        or now >= claim.lease_expires_at
    ):
        raise ExecutionJobError("release_execution_lease_lost", "执行租约已失效，拒绝迟到结果")


def _finish(row: ReleaseExecutionJobRow, status: str, now: datetime, code: str | None) -> None:
    row.status, row.updated_at, row.completed_at, row.last_error_code = status, now, now, code
    row.locked_by = row.lease_token = row.locked_until = None
