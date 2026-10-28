"""独立专业任务的有限租约尝试与原子结果发布。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.execution.release.observer import utc_now
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_coordination.source import check_current_basis
from opsswarm.investigation.reinvestigation_dispatch.tables import ReflowDispatchTaskRow
from opsswarm.investigation.reinvestigation_execution.contracts import (
    EffectiveWindow,
    ExecutionClaim,
    ExecutionResult,
)
from opsswarm.investigation.reinvestigation_execution.source import (
    TaskSource,
    load_source,
    lock_source,
    require_current,
)
from opsswarm.investigation.reinvestigation_execution.tables import (
    ExecutionAttemptRow,
    ExecutionEvidenceRow,
    ExecutionPacketRow,
    ExecutionResultRow,
    ExecutionWorkRow,
)
from opsswarm.investigation.reinvestigation_execution.window import effective_window


def finish(work: ExecutionWorkRow, status: str, now: datetime, error: str | None) -> None:
    work.status, work.updated_at, work.completed_at, work.last_error = status, now, now, error
    work.locked_by = work.lease_token = work.locked_until = None


def require_lease(work: ExecutionWorkRow, token: UUID, now: datetime) -> None:
    if (
        work.status != "running"
        or work.lease_token != token
        or work.locked_until is None
        or now >= work.locked_until
    ):
        raise ReflowError("execution_lease_lost", "专业执行租约失效，拒绝迟到结果")


async def close_calls(session: AsyncSession, attempt_id: UUID, now: datetime) -> None:
    await session.execute(
        update(CallReservationRow)
        .where(
            CallReservationRow.reflow_task_attempt_id == attempt_id,
            CallReservationRow.status == "reserved",
        )
        .values(status="unknown", error_code="reflow_attempt_ended", finished_at=now)
    )


async def active_claim(
    session: AsyncSession, claim: ExecutionClaim, *, clock: Callable[[], datetime]
) -> tuple[ExecutionWorkRow, ExecutionAttemptRow, InvestigationRunRow]:
    run = await lock_wait_run(session, claim.investigation_run_id, read=False)
    work = await session.scalar(
        select(ExecutionWorkRow)
        .where(
            ExecutionWorkRow.task_id == claim.task_id,
            ExecutionWorkRow.investigation_run_id == run.id,
        )
        .with_for_update()
    )
    if work is None:
        raise ReflowError("execution_not_found", "专业执行任务不存在", 404)
    await lock_source(session, run)
    require_lease(work, claim.lease_token, normalized_now(clock))
    attempt = await session.get(ExecutionAttemptRow, claim.attempt_id)
    if (
        attempt is None
        or attempt.task_id != work.task_id
        or attempt.investigation_run_id != run.id
        or attempt.number != claim.number
        or work.attempt_count != claim.number
        or attempt.status != "running"
        or attempt.lease_token != claim.lease_token
        or attempt.worker_id != work.locked_by
        or attempt.lease_expires_at != claim.lease_expires_at
        or attempt.lease_expires_at != work.locked_until
    ):
        raise ReflowError("execution_lease_lost", "执行尝试、任务和当前租约不匹配")
    return work, attempt, run


class ExecutionStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ):
        self._factory, self._clock = factory, clock

    async def prepare_next(self) -> bool:
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                task = await session.scalar(
                    select(ReflowDispatchTaskRow)
                    .where(
                        ReflowDispatchTaskRow.id.not_in(select(ExecutionWorkRow.task_id)),
                        ReflowDispatchTaskRow.id.not_in(skipped),
                    )
                    .order_by(ReflowDispatchTaskRow.created_at, ReflowDispatchTaskRow.id)
                    .limit(1)
                )
                if task is None:
                    return False
                skipped.append(task.id)
                parent = await session.scalar(
                    select(WorkItemRow)
                    .where(WorkItemRow.investigation_run_id == task.investigation_run_id)
                    .with_for_update(skip_locked=True)
                )
                if parent is None or await session.get(ExecutionWorkRow, task.id) is not None:
                    continue
                run = await session.get(InvestigationRunRow, task.investigation_run_id)
                assert run is not None
                await lock_source(session, run)
                source = await load_source(session, run, task.id, clock=self._clock)
                window, reason = None, None
                try:
                    await check_current_basis(
                        session, run, source.source, now=normalized_now(self._clock)
                    )
                    window = effective_window(source.assignment)
                except (CallBudgetExceeded, ReflowError) as error:
                    if error.code not in (
                        "reflow_source_changed",
                        "run_deadline_exceeded",
                        "execution_window_too_short",
                    ):
                        raise
                    reason = error.code
                now = normalized_now(self._clock)
                session.add(
                    ExecutionWorkRow(
                        task_id=task.id,
                        investigation_run_id=run.id,
                        dispatch_sha256=source.dispatch_sha256,
                        assignment_sha256=content_hash(source.assignment),
                        policy_version="reinvestigation-task-v1",
                        evidence_policy_version="reflow-live-mcp-v1",
                        effective_window=window.model_dump(mode="json") if window else None,
                        status="blocked" if reason else "queued",
                        attempt_count=0,
                        max_attempts=source.assignment.max_attempts,
                        available_at=now,
                        created_at=now,
                        updated_at=now,
                        completed_at=now if reason else None,
                        last_error=reason,
                        locked_by=None,
                        lease_token=None,
                        locked_until=None,
                    )
                )
                await session.flush()
                return True
        return False

    async def claim_next(
        self, *, worker_id: str, lease_duration: timedelta = timedelta(seconds=180)
    ) -> ExecutionClaim | None:
        if (
            not worker_id.strip()
            or len(worker_id) > 100
            or not timedelta(seconds=1) <= lease_duration <= timedelta(seconds=300)
        ):
            raise ValueError("Worker身份或专业执行租约时长无效")
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                now = normalized_now(self._clock)
                candidate = await session.scalar(
                    select(ExecutionWorkRow)
                    .where(
                        ExecutionWorkRow.task_id.not_in(skipped),
                        or_(
                            and_(
                                ExecutionWorkRow.status == "queued",
                                ExecutionWorkRow.available_at <= now,
                            ),
                            and_(
                                ExecutionWorkRow.status == "running",
                                ExecutionWorkRow.locked_until <= now,
                            ),
                        ),
                    )
                    .order_by(ExecutionWorkRow.available_at, ExecutionWorkRow.task_id)
                    .limit(1)
                )
                if candidate is None:
                    return None
                skipped.append(candidate.task_id)
                parent = await session.scalar(
                    select(WorkItemRow)
                    .where(WorkItemRow.investigation_run_id == candidate.investigation_run_id)
                    .with_for_update(skip_locked=True)
                )
                if parent is None:
                    continue
                work = await session.scalar(
                    select(ExecutionWorkRow)
                    .where(ExecutionWorkRow.task_id == candidate.task_id)
                    .with_for_update()
                    .execution_options(populate_existing=True)
                )
                assert work is not None
                run = await session.get(InvestigationRunRow, work.investigation_run_id)
                assert run is not None
                await lock_source(session, run)
                now = normalized_now(self._clock)
                if work.status == "running" and work.locked_until and work.locked_until <= now:
                    old = await session.scalar(
                        select(ExecutionAttemptRow).where(
                            ExecutionAttemptRow.task_id == work.task_id,
                            ExecutionAttemptRow.number == work.attempt_count,
                        )
                    )
                    if old is None or old.status != "running":
                        raise ReflowError("execution_integrity_violation", "失租执行缺少原运行尝试")
                    await close_calls(session, old.id, now)
                    old.status, old.finished_at, old.error_code = (
                        "lease_expired",
                        now,
                        "execution_lease_expired",
                    )
                    work.status, work.updated_at, work.available_at = "queued", now, now
                    work.locked_by = work.lease_token = work.locked_until = None
                    await session.flush()
                elif work.status != "queued" or work.available_at > now:
                    continue
                if work.attempt_count >= work.max_attempts:
                    finish(work, "failed", now, "execution_attempts_exhausted")
                    continue
                count = await session.scalar(
                    select(func.count())
                    .select_from(ExecutionWorkRow)
                    .where(
                        ExecutionWorkRow.investigation_run_id == run.id,
                        ExecutionWorkRow.status == "running",
                        ExecutionWorkRow.locked_until > now,
                    )
                )
                if (count or 0) >= 3:
                    continue
                try:
                    await require_current(session, run, work, clock=self._clock)
                except (ReflowError, CallBudgetExceeded) as error:
                    if error.code not in ("reflow_source_changed", "run_deadline_exceeded"):
                        raise
                    finish(work, "failed", normalized_now(self._clock), error.code)
                    continue
                now, token = normalized_now(self._clock), uuid4()
                work.attempt_count += 1
                work.status, work.locked_by, work.lease_token = "running", worker_id, token
                work.locked_until, work.updated_at = now + lease_duration, now
                work.last_error, work.completed_at = None, None
                await session.flush()
                attempt = ExecutionAttemptRow(
                    id=uuid4(),
                    task_id=work.task_id,
                    investigation_run_id=run.id,
                    number=work.attempt_count,
                    worker_id=worker_id,
                    lease_token=token,
                    status="running",
                    started_at=now,
                    lease_expires_at=work.locked_until,
                    finished_at=None,
                    error_code=None,
                )
                session.add(attempt)
                await session.flush()
                require_lease(work, token, normalized_now(self._clock))
                return ExecutionClaim(
                    task_id=work.task_id,
                    investigation_run_id=run.id,
                    attempt_id=attempt.id,
                    number=attempt.number,
                    lease_token=token,
                    lease_expires_at=work.locked_until,
                )
        return None

    async def context(self, claim: ExecutionClaim) -> tuple[TaskSource, EffectiveWindow]:
        from opsswarm.investigation.reinvestigation_execution.source import validate_work

        async with self._factory() as session, session.begin():
            work, _, run = await active_claim(session, claim, clock=self._clock)
            source = await require_current(session, run, work, clock=self._clock)
            window = validate_work(work, source)
            assert window is not None
            return source, window

    async def complete(self, claim: ExecutionClaim, model_call_id: UUID) -> None:
        from opsswarm.investigation.reinvestigation_execution.validation import validated_model_call

        async with self._factory() as session, session.begin():
            work, attempt, run = await active_claim(session, claim, clock=self._clock)
            source = await require_current(session, run, work, clock=self._clock)
            model = await validated_model_call(session, work, model_call_id, source)
            from opsswarm.investigation.budget.queries import read_budget_snapshot
            from opsswarm.investigation.reinvestigation_execution.policy import check_budget

            check_budget(
                source.source,
                await read_budget_snapshot(session, run),
                task_id=work.task_id,
                now=normalized_now(self._clock),
            )
            evidence = await session.get(ExecutionEvidenceRow, work.task_id)
            packet = await session.get(ExecutionPacketRow, work.task_id)
            assert evidence is not None and packet is not None
            now, deadline = normalized_now(self._clock), work.locked_until
            result = ExecutionResult(
                investigation_run_id=run.id,
                task_id=work.task_id,
                dispatch_sha256=work.dispatch_sha256,
                evidence_call_id=evidence.call_id,
                model_call_id=model_call_id,
                evidence_sha256=evidence.snapshot_sha256,
                packet_sha256=packet.packet_sha256,
                report=model.report,
                created_at=now,
            )
            session.add(
                ExecutionResultRow(
                    task_id=work.task_id,
                    investigation_run_id=run.id,
                    attempt_id=attempt.id,
                    content=result.model_dump(mode="json"),
                    content_sha256=content_hash(result),
                    created_at=now,
                )
            )
            await session.flush()
            attempt.status, attempt.finished_at = "succeeded", now
            finish(work, "succeeded", now, None)
            await session.flush()
            if deadline is None or normalized_now(self._clock) >= deadline:
                raise ReflowError("execution_lease_lost", "专业结果提交时租约超期")
            await check_current_basis(session, run, source.source, now=normalized_now(self._clock))

    async def fail(self, claim: ExecutionClaim, *, error_code: str, retryable: bool) -> None:
        async with self._factory() as session, session.begin():
            work, attempt, _ = await active_claim(session, claim, clock=self._clock)
            now, deadline = normalized_now(self._clock), work.locked_until
            await close_calls(session, attempt.id, now)
            attempt.status, attempt.finished_at, attempt.error_code = "failed", now, error_code
            if retryable and work.attempt_count < work.max_attempts:
                work.status, work.updated_at, work.available_at = (
                    "queued",
                    now,
                    now + timedelta(seconds=2),
                )
                work.last_error = error_code
                work.locked_by = work.lease_token = work.locked_until = None
            else:
                finish(work, "failed", now, error_code)
            await session.flush()
            if deadline is None or normalized_now(self._clock) >= deadline:
                raise ReflowError("execution_lease_lost", "专业失败结算时租约超期")
