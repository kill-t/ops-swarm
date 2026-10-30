"""回流候选评估任务的独立租约、有限尝试与成功工件原子结算。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.policy import CallBudgetExceeded, remaining_time
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow
from opsswarm.investigation.reinvestigation.tables import CandidateReinvestigationRow
from opsswarm.investigation.reinvestigation_candidate.context import build_packet
from opsswarm.investigation.reinvestigation_candidate.contracts import (
    POLICY_VERSION,
    ReflowCandidateClaim,
    ReflowCandidateError,
    ReflowCandidatePacket,
    ReflowCandidateResult,
    ReflowCandidateSource,
)
from opsswarm.investigation.reinvestigation_candidate.policy import (
    check_budget,
    evaluate_candidate,
)
from opsswarm.investigation.reinvestigation_candidate.queries import validated_call
from opsswarm.investigation.reinvestigation_candidate.source import load_source, require_current
from opsswarm.investigation.reinvestigation_candidate.tables import (
    ReflowCandidateAttemptRow,
    ReflowCandidateJobRow,
    ReflowCandidateResultRow,
)
from opsswarm.investigation.reinvestigation_routing.tables import ReflowRouteRow


def utc_now() -> datetime:
    return datetime.now(UTC)


class ReflowCandidateJobStore:
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
                        CandidateReinvestigationRow.investigation_run_id.in_(
                            select(ReflowRouteRow.investigation_run_id).where(
                                ReflowRouteRow.route == "candidate_evaluation"
                            )
                        ),
                        CandidateReinvestigationRow.investigation_run_id.not_in(
                            select(ReflowCandidateJobRow.investigation_run_id)
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
                if await session.get(ReflowCandidateJobRow, run_id) is not None:
                    continue
                run = await session.get(InvestigationRunRow, run_id)
                if run is None:
                    raise ReflowCandidateError(
                        "reflow_candidate_integrity_violation", "回流阶段缺少原运行"
                    )
                await _lock_basis(session, run)
                now, source, packet, blocked = self._now(), None, None, None
                try:
                    source = await load_source(
                        session, run, admission, created_at=now, clock=self._clock
                    )
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
                except ReflowCandidateError as error:
                    if error.code not in (
                        "reflow_source_changed",
                        "run_deadline_exceeded",
                    ):
                        raise
                    blocked = error.code
                now = self._now()
                session.add(
                    ReflowCandidateJobRow(
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
    ) -> ReflowCandidateClaim | None:
        if (
            not worker_id.strip()
            or len(worker_id) > 100
            or not timedelta(seconds=1) <= lease_duration <= timedelta(seconds=300)
        ):
            raise ValueError("回流候选评估 Worker 标识或租约时长不合法")
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                now = self._now()
                candidate = (
                    await session.execute(
                        select(ReflowCandidateJobRow.id, ReflowCandidateJobRow.investigation_run_id)
                        .where(
                            ReflowCandidateJobRow.policy_version == POLICY_VERSION,
                            ReflowCandidateJobRow.id.not_in(skipped),
                            or_(
                                and_(
                                    ReflowCandidateJobRow.status == "queued",
                                    ReflowCandidateJobRow.available_at <= now,
                                ),
                                and_(
                                    ReflowCandidateJobRow.status == "running",
                                    ReflowCandidateJobRow.locked_until <= now,
                                ),
                            ),
                        )
                        .order_by(
                            ReflowCandidateJobRow.available_at,
                            ReflowCandidateJobRow.created_at,
                            ReflowCandidateJobRow.id,
                        )
                        .limit(1)
                    )
                ).one_or_none()
                if candidate is None:
                    return None
                job_id, run_id = candidate
                skipped.append(job_id)
                # 所有路径都先父 work 后回流候选评估 job，领取不等待正在处理的父事务。
                work = await session.scalar(
                    select(WorkItemRow)
                    .where(WorkItemRow.investigation_run_id == run_id)
                    .with_for_update(skip_locked=True)
                )
                if work is None:
                    continue
                job = await session.scalar(
                    select(ReflowCandidateJobRow)
                    .where(ReflowCandidateJobRow.id == job_id)
                    .with_for_update()
                )
                assert job is not None
                run = await session.get(InvestigationRunRow, run_id)
                if run is None:
                    raise ReflowCandidateError(
                        "reflow_candidate_integrity_violation", "回流任务缺少原运行"
                    )
                await _lock_basis(session, run)
                now = self._now()
                if job.status == "running":
                    if job.locked_until is None or job.locked_until > now:
                        continue
                    previous = await session.scalar(
                        select(ReflowCandidateAttemptRow).where(
                            ReflowCandidateAttemptRow.job_id == job.id,
                            ReflowCandidateAttemptRow.number == job.attempt_count,
                        )
                    )
                    if previous is None or previous.status != "running":
                        raise ReflowCandidateError(
                            "reflow_candidate_integrity_violation", "过期回流候选评估缺少运行尝试"
                        )
                    await close_attempt_calls(session, previous.id, now)
                    previous.status, previous.finished_at = "lease_expired", now
                    previous.error_code = "reflow_candidate_lease_expired"
                elif job.status != "queued" or job.available_at > now:
                    continue
                if job.attempt_count >= 3:
                    _finish_job(job, "failed", now, "reflow_candidate_attempts_exhausted")
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
                except (ReflowCandidateError, CallBudgetExceeded) as error:
                    if isinstance(error, ReflowCandidateError) and error.code not in (
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
                attempt = ReflowCandidateAttemptRow(
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
                return ReflowCandidateClaim(
                    job_id=job.id,
                    investigation_run_id=run_id,
                    attempt_id=attempt_id,
                    number=job.attempt_count,
                    lease_token=token,
                    lease_expires_at=job.locked_until,
                )
        return None

    async def get_packet(
        self, claim: ReflowCandidateClaim
    ) -> tuple[ReflowCandidateSource, ReflowCandidatePacket]:
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

    async def complete(self, claim: ReflowCandidateClaim, call_id: UUID) -> None:
        async with self._factory() as session, session.begin():
            job, attempt, run = await _active_claim(session, claim, self._now(), clock=self._clock)
            source, packet = await require_current(session, run, job, clock=self._clock)
            call = await session.get(CallReservationRow, call_id)
            if call is None:
                raise ReflowCandidateError("reflow_candidate_call_invalid", "回流成功调用不存在")
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
            evaluation = evaluate_candidate(source, model_result)
            result = ReflowCandidateResult(
                investigation_run_id=run.id,
                job_id=job.id,
                source_call_id=call_id,
                source_sha256=content_hash(source),
                evaluation=evaluation,
                routing_sha256=source.routing_sha256,
                created_at=now,
            )
            session.add(
                ReflowCandidateResultRow(
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
                raise ReflowCandidateError(
                    "reflow_candidate_lease_lost", "回流候选评估提交阶段租约已过期"
                )
            remaining_time(source.call_budget, source.started_at, self._now())

    async def fail(self, claim: ReflowCandidateClaim, *, error_code: str, retryable: bool) -> None:
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
                raise ReflowCandidateError(
                    "reflow_candidate_lease_lost", "回流候选评估失败结算阶段租约已过期"
                )


def _require_lease(job: ReflowCandidateJobRow, token: object, now: datetime) -> None:
    if (
        job.status != "running"
        or job.lease_token != token
        or job.locked_until is None
        or job.locked_until <= now
    ):
        raise ReflowCandidateError(
            "reflow_candidate_lease_lost", "回流候选评估租约无效，拒绝迟到结果"
        )


async def _active_claim(
    session: AsyncSession,
    claim: ReflowCandidateClaim,
    now: datetime,
    *,
    clock: Callable[[], datetime] = utc_now,
) -> tuple[ReflowCandidateJobRow, ReflowCandidateAttemptRow, InvestigationRunRow]:
    run = await lock_wait_run(session, claim.investigation_run_id, read=False)
    job = await session.scalar(
        select(ReflowCandidateJobRow)
        .where(
            ReflowCandidateJobRow.id == claim.job_id,
            ReflowCandidateJobRow.investigation_run_id == run.id,
        )
        .with_for_update()
    )
    if job is None:
        raise ReflowCandidateError("reflow_candidate_job_not_found", "回流候选评估任务不存在", 404)
    await _lock_basis(session, run)
    now = normalized_now(clock)
    _require_lease(job, claim.lease_token, now)
    attempt = await session.get(ReflowCandidateAttemptRow, claim.attempt_id)
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
        raise ReflowCandidateError("reflow_candidate_lease_lost", "回流候选评估尝试不属于有效租约")
    return job, attempt, run


def _finish_job(
    job: ReflowCandidateJobRow, status: str, now: datetime, error_code: str | None
) -> None:
    job.status, job.updated_at, job.completed_at = status, now, now
    job.locked_by = job.lease_token = job.locked_until = None
    job.last_error = error_code


async def close_attempt_calls(session: AsyncSession, attempt_id: UUID, now: datetime) -> None:
    """只收尾本阶段失联预留，保留既有分配和实际用量。"""
    from sqlalchemy import update

    await session.execute(
        update(CallReservationRow)
        .where(
            CallReservationRow.reflow_candidate_attempt_id == attempt_id,
            CallReservationRow.status == "reserved",
        )
        .values(status="unknown", error_code="reflow_candidate_attempt_ended", finished_at=now)
    )


async def _lock_basis(session: AsyncSession, run: InvestigationRunRow) -> None:
    from opsswarm.investigation.reinvestigation_review.job_store import (
        _lock_basis as lock_review_basis,
    )
    from opsswarm.investigation.reinvestigation_review.tables import ReflowReviewJobRow

    await session.scalar(
        select(ReflowReviewJobRow).where(ReflowReviewJobRow.id == run.id).with_for_update()
    )
    await lock_review_basis(session, run)
