"""准备任务的独立租约、有限尝试与成功工件原子结算。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_prepare.builder import build_prepared_input, make_artifact
from opsswarm.investigation.human_prepare.contracts import (
    POLICY_VERSION,
    PreparationClaim,
    PreparationError,
    PreparationSource,
    PreparedInput,
)
from opsswarm.investigation.human_prepare.queries import validated_source
from opsswarm.investigation.human_prepare.store import utc_now
from opsswarm.investigation.human_prepare.tables import (
    HumanPreparationArtifactRow,
    HumanPreparationAttemptRow,
    HumanPreparationJobRow,
)
from opsswarm.investigation.human_wait.state import load_wait_state, lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow


class HumanPreparationJobStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock

    def _now(self) -> datetime:
        return normalized_now(self._clock)

    async def claim_next(
        self, *, worker_id: str, lease_duration: timedelta = timedelta(seconds=600)
    ) -> PreparationClaim | None:
        if (
            not worker_id.strip()
            or len(worker_id) > 100
            or not timedelta(seconds=1) <= lease_duration <= timedelta(seconds=900)
        ):
            raise ValueError("准备 Worker 标识或租约时长不合法")
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                now = self._now()
                candidate = (
                    await session.execute(
                        select(
                            HumanPreparationJobRow.id, HumanPreparationJobRow.investigation_run_id
                        )
                        .where(
                            HumanPreparationJobRow.policy_version == POLICY_VERSION,
                            HumanPreparationJobRow.id.not_in(skipped),
                            or_(
                                and_(
                                    HumanPreparationJobRow.status == "queued",
                                    HumanPreparationJobRow.available_at <= now,
                                ),
                                and_(
                                    HumanPreparationJobRow.status == "running",
                                    HumanPreparationJobRow.locked_until <= now,
                                ),
                            ),
                        )
                        .order_by(
                            HumanPreparationJobRow.available_at,
                            HumanPreparationJobRow.created_at,
                            HumanPreparationJobRow.id,
                        )
                        .limit(1)
                    )
                ).one_or_none()
                if candidate is None:
                    return None
                job_id, run_id = candidate
                skipped.append(job_id)
                # 所有路径都先父 work 后准备 job，领取不等待正在处理的父事务。
                work = await session.scalar(
                    select(WorkItemRow)
                    .where(WorkItemRow.investigation_run_id == run_id)
                    .with_for_update(skip_locked=True)
                )
                if work is None:
                    continue
                job = await session.scalar(
                    select(HumanPreparationJobRow)
                    .where(HumanPreparationJobRow.id == job_id)
                    .with_for_update()
                )
                assert job is not None
                now = self._now()
                if job.status == "running":
                    if job.locked_until is None or job.locked_until > now:
                        continue
                    previous = await session.scalar(
                        select(HumanPreparationAttemptRow).where(
                            HumanPreparationAttemptRow.job_id == job.id,
                            HumanPreparationAttemptRow.number == job.attempt_count,
                        )
                    )
                    if previous is None or previous.status != "running":
                        raise PreparationError(
                            "human_preparation_integrity_violation", "过期准备缺少运行尝试"
                        )
                    previous.status, previous.finished_at = "lease_expired", now
                    previous.error_code = "human_preparation_lease_expired"
                elif job.status != "queued" or job.available_at > now:
                    continue
                if job.attempt_count >= 3:
                    _finish_job(job, "failed", now, "human_preparation_attempts_exhausted")
                    continue
                token, attempt_id = uuid4(), uuid4()
                job.attempt_count += 1
                job.status, job.locked_by, job.lease_token = "running", worker_id, token
                job.locked_until, job.updated_at = now + lease_duration, now
                job.completed_at, job.last_error_code = None, None
                attempt = HumanPreparationAttemptRow(
                    id=attempt_id,
                    job_id=job.id,
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
                return PreparationClaim(
                    job_id=job.id,
                    investigation_run_id=run_id,
                    attempt_id=attempt_id,
                    number=job.attempt_count,
                    lease_token=token,
                    lease_expires_at=job.locked_until,
                )
        return None

    async def get_source(self, claim: PreparationClaim) -> PreparationSource:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now())
            source = await validated_source(session, job, run)
            await _require_current(session, run, job)
            _require_lease(job, claim.lease_token, self._now())
            return source

    async def complete(self, claim: PreparationClaim, prepared: PreparedInput) -> None:
        async with self._factory() as session, session.begin():
            job, attempt, run = await _active_claim(session, claim, self._now())
            source = await validated_source(session, job, run)
            await _require_current(session, run, job)
            if build_prepared_input(source) != prepared:
                raise PreparationError("human_preparation_input_conflict", "准备结果不匹配完整来源")
            budget = await read_budget_snapshot(session, run)
            now = self._now()
            _require_lease(job, claim.lease_token, now)
            deadline = job.locked_until
            artifact = make_artifact(source, prepared, budget, now=now)
            session.add(
                HumanPreparationArtifactRow(
                    job_id=job.id,
                    attempt_id=attempt.id,
                    version=artifact.version,
                    content=artifact.model_dump(mode="json"),
                    content_sha256=content_hash(artifact),
                    created_at=now,
                )
            )
            # 原生准入先核验仍有效的租约，延迟约束再核对最终成功状态。
            await session.flush()
            attempt.status, attempt.finished_at = "succeeded", now
            _finish_job(job, "succeeded", now, None)
            await session.flush()
            if deadline is None or self._now() >= deadline:
                raise PreparationError("human_preparation_lease_lost", "准备提交阶段租约已过期")

    async def fail(self, claim: PreparationClaim, *, error_code: str, retryable: bool) -> None:
        async with self._factory() as session, session.begin():
            job, attempt, _ = await _active_claim(session, claim, self._now())
            now, deadline = self._now(), job.locked_until
            _require_lease(job, claim.lease_token, now)
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
                raise PreparationError("human_preparation_lease_lost", "准备失败结算阶段租约已过期")


def _require_lease(job: HumanPreparationJobRow, token: object, now: datetime) -> None:
    if (
        job.status != "running"
        or job.lease_token != token
        or job.locked_until is None
        or job.locked_until <= now
    ):
        raise PreparationError("human_preparation_lease_lost", "准备租约无效，拒绝迟到结果")


async def _active_claim(
    session: AsyncSession, claim: PreparationClaim, now: datetime
) -> tuple[HumanPreparationJobRow, HumanPreparationAttemptRow, InvestigationRunRow]:
    run = await lock_wait_run(session, claim.investigation_run_id, read=False)
    job = await session.scalar(
        select(HumanPreparationJobRow)
        .where(
            HumanPreparationJobRow.id == claim.job_id,
            HumanPreparationJobRow.investigation_run_id == run.id,
        )
        .with_for_update()
    )
    if job is None:
        raise PreparationError("human_preparation_job_not_found", "准备任务不存在", 404)
    _require_lease(job, claim.lease_token, now)
    attempt = await session.get(HumanPreparationAttemptRow, claim.attempt_id)
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
        raise PreparationError("human_preparation_lease_lost", "准备尝试不属于有效租约")
    return job, attempt, run


async def _require_current(
    session: AsyncSession, run: InvestigationRunRow, job: HumanPreparationJobRow
) -> None:
    _, wait = await load_wait_state(session, run.id)
    if (
        run.status != "await_human"
        or wait.status != "waiting"
        or wait.state_version != job.wait_version
    ):
        raise PreparationError("human_preparation_wait_changed", "补充或取消已改变待准备输入")
    incident = await session.get(IncidentRow, run.incident_id)
    if incident is None or incident.status != "investigating":
        raise PreparationError("human_preparation_wait_changed", "事故不再允许准备")


def _finish_job(
    job: HumanPreparationJobRow, status: str, now: datetime, error_code: str | None
) -> None:
    job.status, job.updated_at, job.completed_at = status, now, now
    job.locked_by = job.lease_token = job.locked_until = None
    job.last_error_code = error_code
