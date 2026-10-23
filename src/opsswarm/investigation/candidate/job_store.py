"""候选评估任务的独立租约、有限尝试与成功工件原子结算。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded, remaining_time
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.candidate.budget import check_budget
from opsswarm.investigation.candidate.contracts import (
    POLICY_VERSION,
    CandidateClaim,
    CandidateError,
    CandidatePacket,
    CandidateSource,
)
from opsswarm.investigation.candidate.evaluation import evaluate_candidate
from opsswarm.investigation.candidate.output import CandidateModelResult
from opsswarm.investigation.candidate.queries import job_source, validated_call
from opsswarm.investigation.candidate.store import utc_now
from opsswarm.investigation.candidate.tables import (
    CandidateAttemptRow,
    CandidateJobRow,
    CandidateResultRow,
)
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow


class CandidateJobStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock

    def _now(self) -> datetime:
        return normalized_now(self._clock)

    async def claim_next(
        self, *, worker_id: str, lease_duration: timedelta = timedelta(seconds=600)
    ) -> CandidateClaim | None:
        if (
            not worker_id.strip()
            or len(worker_id) > 100
            or not timedelta(seconds=1) <= lease_duration <= timedelta(seconds=900)
        ):
            raise ValueError("候选评估 Worker 标识或租约时长不合法")
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                now = self._now()
                candidate = (
                    await session.execute(
                        select(CandidateJobRow.id, CandidateJobRow.investigation_run_id)
                        .where(
                            CandidateJobRow.policy_version == POLICY_VERSION,
                            CandidateJobRow.id.not_in(skipped),
                            or_(
                                and_(
                                    CandidateJobRow.status == "queued",
                                    CandidateJobRow.available_at <= now,
                                ),
                                and_(
                                    CandidateJobRow.status == "running",
                                    CandidateJobRow.locked_until <= now,
                                ),
                            ),
                        )
                        .order_by(
                            CandidateJobRow.available_at,
                            CandidateJobRow.created_at,
                            CandidateJobRow.id,
                        )
                        .limit(1)
                    )
                ).one_or_none()
                if candidate is None:
                    return None
                job_id, run_id = candidate
                skipped.append(job_id)
                # 所有路径都先父 work 后候选评估 job，领取不等待正在处理的父事务。
                work = await session.scalar(
                    select(WorkItemRow)
                    .where(WorkItemRow.investigation_run_id == run_id)
                    .with_for_update(skip_locked=True)
                )
                if work is None:
                    continue
                job = await session.scalar(
                    select(CandidateJobRow).where(CandidateJobRow.id == job_id).with_for_update()
                )
                assert job is not None
                now = self._now()
                if job.status == "running":
                    if job.locked_until is None or job.locked_until > now:
                        continue
                    previous = await session.scalar(
                        select(CandidateAttemptRow).where(
                            CandidateAttemptRow.job_id == job.id,
                            CandidateAttemptRow.number == job.attempt_count,
                        )
                    )
                    if previous is None or previous.status != "running":
                        raise CandidateError(
                            "candidate_integrity_violation", "过期候选评估缺少运行尝试"
                        )
                    await close_attempt_calls(session, previous.id, now)
                    previous.status, previous.finished_at = "lease_expired", now
                    previous.error_code = "candidate_lease_expired"
                elif job.status != "queued" or job.available_at > now:
                    continue
                if job.attempt_count >= 3:
                    _finish_job(job, "failed", now, "candidate_attempts_exhausted")
                    continue
                run = await session.get(InvestigationRunRow, run_id)
                if run is None:
                    raise CandidateError("candidate_integrity_violation", "候选任务缺少原运行")
                try:
                    source, packet = await _current_packet(session, run, job, clock=self._clock)
                    check_budget(
                        source,
                        packet,
                        await read_budget_snapshot(session, run),
                        now=self._now(),
                        allocate=False,
                    )
                except (CandidateError, CallBudgetExceeded) as error:
                    if (
                        isinstance(error, CandidateError)
                        and error.code != "candidate_source_changed"
                    ):
                        raise
                    _finish_job(job, "failed", self._now(), error.code)
                    continue
                now = self._now()
                token, attempt_id = uuid4(), uuid4()
                job.attempt_count += 1
                job.status, job.locked_by, job.lease_token = "running", worker_id, token
                job.locked_until, job.updated_at = now + lease_duration, now
                job.completed_at, job.last_error_code = None, None
                attempt = CandidateAttemptRow(
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
                return CandidateClaim(
                    job_id=job.id,
                    investigation_run_id=run_id,
                    attempt_id=attempt_id,
                    number=job.attempt_count,
                    lease_token=token,
                    lease_expires_at=job.locked_until,
                )
        return None

    async def get_packet(self, claim: CandidateClaim) -> tuple[CandidateSource, CandidatePacket]:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now())
            source, packet = await _current_packet(session, run, job, clock=self._clock)
            check_budget(
                source,
                packet,
                await read_budget_snapshot(session, run),
                now=self._now(),
                allocate=False,
            )
            _require_lease(job, claim.lease_token, self._now())
            return source, packet

    async def complete(self, claim: CandidateClaim, call_id: UUID) -> None:
        async with self._factory() as session, session.begin():
            job, attempt, run = await _active_claim(session, claim, self._now())
            source, packet = await _current_packet(session, run, job, clock=self._clock)
            call = await validated_call(session, job, source, packet, call_id)
            check_budget(
                source,
                packet,
                await read_budget_snapshot(session, run),
                now=self._now(),
                allocate=False,
            )
            remaining_time(source.call_budget, source.started_at, self._now())
            now, deadline = self._now(), job.locked_until
            result = evaluate_candidate(
                source, CandidateModelResult.model_validate(call.call_result)
            )
            session.add(
                CandidateResultRow(
                    job_id=job.id,
                    attempt_id=attempt.id,
                    source_call_id=call.id,
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
                raise CandidateError("candidate_lease_lost", "候选评估提交阶段租约已过期")
            remaining_time(source.call_budget, source.started_at, self._now())

    async def fail(self, claim: CandidateClaim, *, error_code: str, retryable: bool) -> None:
        async with self._factory() as session, session.begin():
            job, attempt, _ = await _active_claim(session, claim, self._now())
            now, deadline = self._now(), job.locked_until
            _require_lease(job, claim.lease_token, now)
            await close_attempt_calls(session, attempt.id, now)
            attempt.status, attempt.finished_at, attempt.error_code = "failed", now, error_code
            if retryable and job.attempt_count < 3:
                job.status, job.updated_at = "queued", now
                job.available_at = now + timedelta(seconds=2)
                job.locked_by = job.lease_token = job.locked_until = None
                job.last_error_code = error_code
            else:
                _finish_job(job, "failed", now, error_code)
            await session.flush()
            if deadline is None or self._now() >= deadline:
                raise CandidateError("candidate_lease_lost", "候选评估失败结算阶段租约已过期")


def _require_lease(job: CandidateJobRow, token: object, now: datetime) -> None:
    if (
        job.status != "running"
        or job.lease_token != token
        or job.locked_until is None
        or job.locked_until <= now
    ):
        raise CandidateError("candidate_lease_lost", "候选评估租约无效，拒绝迟到结果")


async def _active_claim(
    session: AsyncSession, claim: CandidateClaim, now: datetime
) -> tuple[CandidateJobRow, CandidateAttemptRow, InvestigationRunRow]:
    run = await lock_wait_run(session, claim.investigation_run_id, read=False)
    job = await session.scalar(
        select(CandidateJobRow)
        .where(
            CandidateJobRow.id == claim.job_id,
            CandidateJobRow.investigation_run_id == run.id,
        )
        .with_for_update()
    )
    if job is None:
        raise CandidateError("candidate_job_not_found", "候选评估任务不存在", 404)
    _require_lease(job, claim.lease_token, now)
    attempt = await session.get(CandidateAttemptRow, claim.attempt_id)
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
        raise CandidateError("candidate_lease_lost", "候选评估尝试不属于有效租约")
    return job, attempt, run


async def _current_packet(
    session: AsyncSession,
    run: InvestigationRunRow,
    job: CandidateJobRow,
    *,
    clock: Callable[[], datetime],
) -> tuple[CandidateSource, CandidatePacket]:
    incident = await session.scalar(
        select(IncidentRow).where(IncidentRow.id == run.incident_id).with_for_update()
    )
    source, packet, current = await job_source(session, job, run, clock=clock)
    if not current or incident is None or incident.status != "investigating":
        raise CandidateError("candidate_source_changed", "候选输入、取消或事故状态已经改变")
    return source, packet


def _finish_job(job: CandidateJobRow, status: str, now: datetime, error_code: str | None) -> None:
    job.status, job.updated_at, job.completed_at = status, now, now
    job.locked_by = job.lease_token = job.locked_until = None
    job.last_error_code = error_code


async def close_attempt_calls(session: AsyncSession, attempt_id: UUID, now: datetime) -> None:
    """只收尾本阶段失联预留，保留既有分配和实际用量。"""
    from sqlalchemy import update

    from opsswarm.investigation.budget.tables import CallReservationRow

    await session.execute(
        update(CallReservationRow)
        .where(
            CallReservationRow.candidate_attempt_id == attempt_id,
            CallReservationRow.status == "reserved",
        )
        .values(status="unknown", error_code="candidate_attempt_ended", finished_at=now)
    )
