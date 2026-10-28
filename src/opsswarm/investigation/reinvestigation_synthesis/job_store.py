"""回流综合任务的独立租约、有限尝试与成功工件原子结算。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.policy import CallBudgetExceeded, remaining_time
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow
from opsswarm.investigation.reinvestigation.tables import CandidateReinvestigationRow
from opsswarm.investigation.reinvestigation_dispatch.tables import ReflowDispatchTaskRow
from opsswarm.investigation.reinvestigation_execution.tables import ExecutionWorkRow
from opsswarm.investigation.reinvestigation_synthesis.context import build_packet
from opsswarm.investigation.reinvestigation_synthesis.contracts import (
    POLICY_VERSION,
    SynthesisClaim,
    SynthesisError,
    SynthesisPacket,
    SynthesisResult,
    SynthesisSource,
)
from opsswarm.investigation.reinvestigation_synthesis.policy import (
    check_budget,
    prepare_blocker,
    validate_proposal,
)
from opsswarm.investigation.reinvestigation_synthesis.queries import validated_call
from opsswarm.investigation.reinvestigation_synthesis.source import load_source, require_current
from opsswarm.investigation.reinvestigation_synthesis.tables import (
    SynthesisAttemptRow,
    SynthesisJobRow,
    SynthesisResultRow,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


class SynthesisJobStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock

    def _now(self) -> datetime:
        return normalized_now(self._clock)

    async def prepare_next(self) -> bool:
        """自动发现唯一准入，失败也冻结独立阶段，避免反复占据队首。"""
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                admission = await session.scalar(
                    select(CandidateReinvestigationRow)
                    .where(
                        # 先过滤尚未闭合的专业集合，避免前八条等待项饿死后面的就绪任务。
                        CandidateReinvestigationRow.investigation_run_id.in_(
                            select(ReflowDispatchTaskRow.investigation_run_id)
                            .outerjoin(
                                ExecutionWorkRow,
                                ExecutionWorkRow.task_id == ReflowDispatchTaskRow.id,
                            )
                            .group_by(ReflowDispatchTaskRow.investigation_run_id)
                            .having(
                                or_(
                                    func.bool_or(
                                        ExecutionWorkRow.status.in_(("failed", "blocked"))
                                    ),
                                    func.bool_and(
                                        func.coalesce(ExecutionWorkRow.status == "succeeded", False)
                                    ),
                                )
                            )
                        ),
                        CandidateReinvestigationRow.investigation_run_id.not_in(
                            select(SynthesisJobRow.investigation_run_id)
                        ),
                        CandidateReinvestigationRow.investigation_run_id.not_in(skipped),
                    )
                    .order_by(
                        CandidateReinvestigationRow.created_at, CandidateReinvestigationRow.id
                    )
                    .limit(1)
                )
                if admission is None:
                    return False
                run_id = admission.investigation_run_id
                skipped.append(run_id)
                work = await session.scalar(
                    select(WorkItemRow)
                    .where(WorkItemRow.investigation_run_id == run_id)
                    .with_for_update(skip_locked=True)
                )
                if work is None:
                    continue
                if await session.get(SynthesisJobRow, run_id) is not None:
                    continue
                run = await session.get(InvestigationRunRow, run_id)
                if run is None:
                    raise SynthesisError(
                        "reflow_synthesis_integrity_violation", "回流阶段缺少原运行"
                    )
                await _lock_basis(session, run)
                state = await session.scalar(
                    text("SELECT reflow_synthesis_professional_state(:id)"), {"id": run_id}
                )
                if state == "pending":
                    continue
                now, source, packet, blocked = self._now(), None, None, None
                if state == "failed":
                    blocked = "synthesis_professionals_failed"
                try:
                    if blocked:
                        raise SynthesisError(blocked, "专业失败，综合不得跳过任务")
                    source = await load_source(
                        session, run, admission, created_at=now, clock=self._clock
                    )
                    blocked = prepare_blocker(source)
                    if blocked is None:
                        packet = build_packet(source)
                        check_budget(
                            source,
                            packet,
                            await read_budget_snapshot(session, run),
                            now=self._now(),
                            allocate=True,
                        )
                except CallBudgetExceeded as error:
                    blocked = error.code
                except SynthesisError as error:
                    if error.code not in (
                        "reflow_source_changed",
                        "run_deadline_exceeded",
                        "synthesis_professionals_failed",
                    ):
                        raise
                    blocked = error.code
                now = self._now()
                session.add(
                    SynthesisJobRow(
                        id=run_id,
                        investigation_run_id=run_id,
                        policy_version=POLICY_VERSION,
                        source=source.model_dump(mode="json") if source else None,
                        source_sha256=content_hash(source) if source else None,
                        packet=packet.model_dump(mode="json") if packet else None,
                        packet_sha256=content_hash(packet) if packet else None,
                        status="blocked" if blocked else "queued",
                        attempt_count=0,
                        available_at=now,
                        created_at=source.created_at if source else now,
                        updated_at=now,
                        completed_at=now if blocked else None,
                        last_error=blocked,
                        locked_by=None,
                        lease_token=None,
                        locked_until=None,
                    )
                )
                await session.flush()
                return True
        return False

    async def claim_next(
        self, *, worker_id: str, lease_duration: timedelta = timedelta(seconds=120)
    ) -> SynthesisClaim | None:
        if (
            not worker_id.strip()
            or len(worker_id) > 100
            or not timedelta(seconds=1) <= lease_duration <= timedelta(seconds=300)
        ):
            raise ValueError("回流综合 Worker 标识或租约时长不合法")
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                now = self._now()
                candidate = (
                    await session.execute(
                        select(SynthesisJobRow.id, SynthesisJobRow.investigation_run_id)
                        .where(
                            SynthesisJobRow.policy_version == POLICY_VERSION,
                            SynthesisJobRow.id.not_in(skipped),
                            or_(
                                and_(
                                    SynthesisJobRow.status == "queued",
                                    SynthesisJobRow.available_at <= now,
                                ),
                                and_(
                                    SynthesisJobRow.status == "running",
                                    SynthesisJobRow.locked_until <= now,
                                ),
                            ),
                        )
                        .order_by(
                            SynthesisJobRow.available_at,
                            SynthesisJobRow.created_at,
                            SynthesisJobRow.id,
                        )
                        .limit(1)
                    )
                ).one_or_none()
                if candidate is None:
                    return None
                job_id, run_id = candidate
                skipped.append(job_id)
                # 所有路径都先父 work 后回流综合 job，领取不等待正在处理的父事务。
                work = await session.scalar(
                    select(WorkItemRow)
                    .where(WorkItemRow.investigation_run_id == run_id)
                    .with_for_update(skip_locked=True)
                )
                if work is None:
                    continue
                job = await session.scalar(
                    select(SynthesisJobRow).where(SynthesisJobRow.id == job_id).with_for_update()
                )
                assert job is not None
                run = await session.get(InvestigationRunRow, run_id)
                if run is None:
                    raise SynthesisError(
                        "reflow_synthesis_integrity_violation", "回流任务缺少原运行"
                    )
                await _lock_basis(session, run)
                now = self._now()
                if job.status == "running":
                    if job.locked_until is None or job.locked_until > now:
                        continue
                    previous = await session.scalar(
                        select(SynthesisAttemptRow).where(
                            SynthesisAttemptRow.job_id == job.id,
                            SynthesisAttemptRow.number == job.attempt_count,
                        )
                    )
                    if previous is None or previous.status != "running":
                        raise SynthesisError(
                            "reflow_synthesis_integrity_violation", "过期回流综合缺少运行尝试"
                        )
                    await close_attempt_calls(session, previous.id, now)
                    previous.status, previous.finished_at = "lease_expired", now
                    previous.error_code = "reflow_synthesis_lease_expired"
                elif job.status != "queued" or job.available_at > now:
                    continue
                if job.attempt_count >= 3:
                    _finish_job(job, "failed", now, "reflow_synthesis_attempts_exhausted")
                    continue
                try:
                    source, packet = await require_current(session, run, job, clock=self._clock)
                    check_budget(
                        source,
                        packet,
                        await read_budget_snapshot(session, run),
                        now=self._now(),
                        allocate=False,
                    )
                except (SynthesisError, CallBudgetExceeded) as error:
                    if isinstance(error, SynthesisError) and error.code not in (
                        "reflow_source_changed",
                        "run_deadline_exceeded",
                    ):
                        raise
                    _finish_job(job, "failed", self._now(), error.code)
                    continue
                now = self._now()
                token, attempt_id = uuid4(), uuid4()
                job.attempt_count += 1
                job.status, job.locked_by, job.lease_token = "running", worker_id, token
                job.locked_until, job.updated_at = now + lease_duration, now
                job.completed_at, job.last_error = None, None
                # 原生尝试守卫读取数据库中的当前租约，不能依赖 ORM 跨表刷新顺序。
                await session.flush()
                attempt = SynthesisAttemptRow(
                    id=attempt_id,
                    job_id=job.id,
                    investigation_run_id=run_id,
                    number=job.attempt_count,
                    lease_token=token,
                    worker_id=worker_id,
                    status="running",
                    started_at=now,
                    lease_expires_at=job.locked_until,
                    finished_at=None,
                    error_code=None,
                )
                session.add(attempt)
                await session.flush()
                _require_lease(job, token, self._now())
                remaining_time(source.call_budget, source.started_at, self._now())
                return SynthesisClaim(
                    job_id=job.id,
                    investigation_run_id=run_id,
                    attempt_id=attempt_id,
                    number=job.attempt_count,
                    lease_token=token,
                    lease_expires_at=job.locked_until,
                )
        return None

    async def get_packet(self, claim: SynthesisClaim) -> tuple[SynthesisSource, SynthesisPacket]:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now(), clock=self._clock)
            source, packet = await require_current(session, run, job, clock=self._clock)
            check_budget(
                source,
                packet,
                await read_budget_snapshot(session, run),
                now=self._now(),
                allocate=False,
            )
            _require_lease(job, claim.lease_token, self._now())
            return source, packet

    async def complete(self, claim: SynthesisClaim, call_id: UUID) -> None:
        async with self._factory() as session, session.begin():
            job, attempt, run = await _active_claim(session, claim, self._now(), clock=self._clock)
            source, packet = await require_current(session, run, job, clock=self._clock)
            call = await session.get(CallReservationRow, call_id)
            if call is None:
                raise SynthesisError("reflow_synthesis_call_invalid", "回流成功调用不存在")
            model_result = await validated_call(session, job, call, source=source, packet=packet)
            check_budget(
                source,
                packet,
                await read_budget_snapshot(session, run),
                now=self._now(),
                allocate=False,
            )
            remaining_time(source.call_budget, source.started_at, self._now())
            now, deadline = self._now(), job.locked_until
            validate_proposal(source, model_result.proposal)
            result = SynthesisResult(
                investigation_run_id=run.id,
                job_id=job.id,
                source_call_id=call_id,
                source_sha256=content_hash(source),
                proposal=model_result.proposal,
                created_at=now,
            )
            session.add(
                SynthesisResultRow(
                    job_id=job.id,
                    investigation_run_id=run.id,
                    attempt_id=attempt.id,
                    source_call_id=call_id,
                    content=result.model_dump(mode="json"),
                    content_sha256=content_hash(result),
                    created_at=now,
                )
            )
            await session.flush()
            attempt.status, attempt.finished_at = "succeeded", now
            _finish_job(job, "succeeded", now, None)
            await session.flush()
            if deadline is None or self._now() >= deadline:
                raise SynthesisError("reflow_synthesis_lease_lost", "回流综合提交阶段租约已过期")
            remaining_time(source.call_budget, source.started_at, self._now())

    async def fail(self, claim: SynthesisClaim, *, error_code: str, retryable: bool) -> None:
        async with self._factory() as session, session.begin():
            job, attempt, _ = await _active_claim(session, claim, self._now(), clock=self._clock)
            now, deadline = self._now(), job.locked_until
            _require_lease(job, claim.lease_token, now)
            await close_attempt_calls(session, attempt.id, now)
            attempt.status, attempt.finished_at, attempt.error_code = "failed", now, error_code
            if retryable and job.attempt_count < 3:
                job.status, job.updated_at = "queued", now
                job.available_at = now + timedelta(seconds=2)
                job.locked_by = job.lease_token = job.locked_until = None
                job.last_error = error_code
            else:
                _finish_job(job, "failed", now, error_code)
            await session.flush()
            if deadline is None or self._now() >= deadline:
                raise SynthesisError(
                    "reflow_synthesis_lease_lost", "回流综合失败结算阶段租约已过期"
                )


def _require_lease(job: SynthesisJobRow, token: object, now: datetime) -> None:
    if (
        job.status != "running"
        or job.lease_token != token
        or job.locked_until is None
        or job.locked_until <= now
    ):
        raise SynthesisError("reflow_synthesis_lease_lost", "回流综合租约无效，拒绝迟到结果")


async def _active_claim(
    session: AsyncSession,
    claim: SynthesisClaim,
    now: datetime,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[SynthesisJobRow, SynthesisAttemptRow, InvestigationRunRow]:
    run = await lock_wait_run(session, claim.investigation_run_id, read=False)
    job = await session.scalar(
        select(SynthesisJobRow)
        .where(
            SynthesisJobRow.id == claim.job_id,
            SynthesisJobRow.investigation_run_id == run.id,
        )
        .with_for_update()
    )
    if job is None:
        raise SynthesisError("reflow_synthesis_job_not_found", "回流综合任务不存在", 404)
    await _lock_basis(session, run)
    now = normalized_now(clock)
    _require_lease(job, claim.lease_token, now)
    attempt = await session.get(SynthesisAttemptRow, claim.attempt_id)
    if (
        job.policy_version != POLICY_VERSION
        or job.attempt_count != claim.number
        or attempt is None
        or attempt.job_id != job.id
        or attempt.investigation_run_id != run.id
        or attempt.worker_id != job.locked_by
        or attempt.number != claim.number
        or attempt.status != "running"
        or attempt.lease_token != claim.lease_token
        or attempt.lease_expires_at != job.locked_until
        or attempt.lease_expires_at != claim.lease_expires_at
    ):
        raise SynthesisError("reflow_synthesis_lease_lost", "回流综合尝试不属于有效租约")
    return job, attempt, run


def _finish_job(job: SynthesisJobRow, status: str, now: datetime, error_code: str | None) -> None:
    job.status, job.updated_at, job.completed_at = status, now, now
    job.locked_by = job.lease_token = job.locked_until = None
    job.last_error = error_code


async def close_attempt_calls(session: AsyncSession, attempt_id: UUID, now: datetime) -> None:
    """只收尾本阶段失联预留，保留既有分配和实际用量。"""
    from sqlalchemy import update

    await session.execute(
        update(CallReservationRow)
        .where(
            CallReservationRow.reflow_synthesis_attempt_id == attempt_id,
            CallReservationRow.status == "reserved",
        )
        .values(status="unknown", error_code="reflow_synthesis_attempt_ended", finished_at=now)
    )


async def _lock_basis(session: AsyncSession, run: InvestigationRunRow) -> None:
    from opsswarm.investigation.reinvestigation_execution.source import lock_source

    await lock_source(session, run)
