"""重评任务的独立租约、有限尝试与成功工件原子结算。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.policy import remaining_time
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_prepare.contracts import PreparationArtifact, PreparationSource
from opsswarm.investigation.human_review.contracts import (
    POLICY_VERSION,
    HumanReviewClaim,
    HumanReviewError,
)
from opsswarm.investigation.human_review.policy import require_committed_capacity
from opsswarm.investigation.human_review.queries import job_source, validated_call
from opsswarm.investigation.human_review.store import utc_now
from opsswarm.investigation.human_review.tables import (
    HumanReviewAttemptRow,
    HumanReviewJobRow,
    HumanReviewResultRow,
)
from opsswarm.investigation.human_routing.persistence import record_route
from opsswarm.investigation.human_routing.store import HumanRoutingStore
from opsswarm.investigation.human_wait.state import load_wait_state, lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow
from opsswarm.investigation.review.contracts import ReviewModelResult


class HumanReviewJobStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock
        self._routing = HumanRoutingStore(factory, clock=clock)

    def _now(self) -> datetime:
        return normalized_now(self._clock)

    async def route_next(self) -> bool:
        return await self._routing.record_next()

    async def claim_next(
        self, *, worker_id: str, lease_duration: timedelta = timedelta(seconds=600)
    ) -> HumanReviewClaim | None:
        if (
            not worker_id.strip()
            or len(worker_id) > 100
            or not timedelta(seconds=1) <= lease_duration <= timedelta(seconds=900)
        ):
            raise ValueError("重评 Worker 标识或租约时长不合法")
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                now = self._now()
                candidate = (
                    await session.execute(
                        select(HumanReviewJobRow.id, HumanReviewJobRow.investigation_run_id)
                        .where(
                            HumanReviewJobRow.policy_version == POLICY_VERSION,
                            HumanReviewJobRow.id.not_in(skipped),
                            or_(
                                and_(
                                    HumanReviewJobRow.status == "queued",
                                    HumanReviewJobRow.available_at <= now,
                                ),
                                and_(
                                    HumanReviewJobRow.status == "running",
                                    HumanReviewJobRow.locked_until <= now,
                                ),
                            ),
                        )
                        .order_by(
                            HumanReviewJobRow.available_at,
                            HumanReviewJobRow.created_at,
                            HumanReviewJobRow.id,
                        )
                        .limit(1)
                    )
                ).one_or_none()
                if candidate is None:
                    return None
                job_id, run_id = candidate
                skipped.append(job_id)
                # 所有路径都先父 work 后重评 job，领取不等待正在处理的父事务。
                work = await session.scalar(
                    select(WorkItemRow)
                    .where(WorkItemRow.investigation_run_id == run_id)
                    .with_for_update(skip_locked=True)
                )
                if work is None:
                    continue
                job = await session.scalar(
                    select(HumanReviewJobRow)
                    .where(HumanReviewJobRow.id == job_id)
                    .with_for_update()
                )
                assert job is not None
                now = self._now()
                if job.status == "running":
                    if job.locked_until is None or job.locked_until > now:
                        continue
                    previous = await session.scalar(
                        select(HumanReviewAttemptRow).where(
                            HumanReviewAttemptRow.job_id == job.id,
                            HumanReviewAttemptRow.number == job.attempt_count,
                        )
                    )
                    if previous is None or previous.status != "running":
                        raise HumanReviewError(
                            "human_review_integrity_violation", "过期重评缺少运行尝试"
                        )
                    await close_attempt_calls(session, previous.id, now)
                    previous.status, previous.finished_at = "lease_expired", now
                    previous.error_code = "human_review_lease_expired"
                elif job.status != "queued" or job.available_at > now:
                    continue
                if job.attempt_count >= 3:
                    _finish_job(job, "failed", now, "human_review_attempts_exhausted")
                    continue
                token, attempt_id = uuid4(), uuid4()
                job.attempt_count += 1
                job.status, job.locked_by, job.lease_token = "running", worker_id, token
                job.locked_until, job.updated_at = now + lease_duration, now
                job.completed_at, job.last_error_code = None, None
                attempt = HumanReviewAttemptRow(
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
                return HumanReviewClaim(
                    job_id=job.id,
                    investigation_run_id=run_id,
                    attempt_id=attempt_id,
                    number=job.attempt_count,
                    lease_token=token,
                    lease_expires_at=job.locked_until,
                )
        return None

    async def get_packet(
        self, claim: HumanReviewClaim
    ) -> tuple[PreparationSource, PreparationArtifact]:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now())
            source, artifact, _ = await job_source(session, job, run, clock=self._clock)
            await _require_current(session, run, job)
            _require_lease(job, claim.lease_token, self._now())
            return source, artifact

    async def complete(self, claim: HumanReviewClaim, call_id: UUID) -> None:
        async with self._factory() as session, session.begin():
            job, attempt, run = await _active_claim(session, claim, self._now())
            source, artifact, _ = await job_source(session, job, run, clock=self._clock)
            await _require_current(session, run, job)
            call = await validated_call(session, job, source, artifact, call_id)
            require_committed_capacity(await read_budget_snapshot(session, run))
            remaining_time(source.call_budget, source.started_at, self._now())
            now, deadline = self._now(), job.locked_until
            result = ReviewModelResult.model_validate(call.call_result)
            session.add(
                HumanReviewResultRow(
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
            await record_route(session, job, run, clock=self._clock)
            if deadline is None or self._now() >= deadline:
                raise HumanReviewError("human_review_lease_lost", "重评提交阶段租约已过期")
            remaining_time(source.call_budget, source.started_at, self._now())

    async def fail(self, claim: HumanReviewClaim, *, error_code: str, retryable: bool) -> None:
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
                raise HumanReviewError("human_review_lease_lost", "重评失败结算阶段租约已过期")


def _require_lease(job: HumanReviewJobRow, token: object, now: datetime) -> None:
    if (
        job.status != "running"
        or job.lease_token != token
        or job.locked_until is None
        or job.locked_until <= now
    ):
        raise HumanReviewError("human_review_lease_lost", "重评租约无效，拒绝迟到结果")


async def _active_claim(
    session: AsyncSession, claim: HumanReviewClaim, now: datetime
) -> tuple[HumanReviewJobRow, HumanReviewAttemptRow, InvestigationRunRow]:
    run = await lock_wait_run(session, claim.investigation_run_id, read=False)
    job = await session.scalar(
        select(HumanReviewJobRow)
        .where(
            HumanReviewJobRow.id == claim.job_id,
            HumanReviewJobRow.investigation_run_id == run.id,
        )
        .with_for_update()
    )
    if job is None:
        raise HumanReviewError("human_review_job_not_found", "重评任务不存在", 404)
    _require_lease(job, claim.lease_token, now)
    attempt = await session.get(HumanReviewAttemptRow, claim.attempt_id)
    if (
        job.policy_version != POLICY_VERSION
        or job.attempt_count != claim.number
        or attempt is None
        or attempt.job_id != job.id
        or attempt.number != claim.number
        or attempt.status != "running"
        or attempt.lease_token != claim.lease_token
        or attempt.lease_expires_at != job.locked_until
        or attempt.lease_expires_at != claim.lease_expires_at
    ):
        raise HumanReviewError("human_review_lease_lost", "重评尝试不属于有效租约")
    return job, attempt, run


async def _require_current(
    session: AsyncSession, run: InvestigationRunRow, job: HumanReviewJobRow
) -> None:
    _, wait = await load_wait_state(session, run.id)
    if (
        run.status != "await_human"
        or wait.status != "waiting"
        or wait.state_version != job.wait_version
    ):
        raise HumanReviewError("human_review_wait_changed", "补充或取消已改变待重评输入")
    incident = await session.get(IncidentRow, run.incident_id)
    if incident is None or incident.status != "investigating":
        raise HumanReviewError("human_review_wait_changed", "事故不再允许重评")


def _finish_job(job: HumanReviewJobRow, status: str, now: datetime, error_code: str | None) -> None:
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
            CallReservationRow.human_review_attempt_id == attempt_id,
            CallReservationRow.status == "reserved",
        )
        .values(status="unknown", error_code="human_review_attempt_ended", finished_at=now)
    )
