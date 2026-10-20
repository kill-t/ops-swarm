"""经验生成任务的持久准入、领取、尝试结算与候选查询。"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.experience.contracts import (
    GENERATION_POLICY_VERSION,
    ExperienceDraft,
    ExperienceError,
    ExperienceSource,
    canonical_hash,
)
from opsswarm.experience.projection import project_source
from opsswarm.experience.records import AttemptView, CandidateView, ExperienceClaim, JobView
from opsswarm.experience.sources import load_source
from opsswarm.experience.tables import (
    ExperienceAttemptRow,
    ExperienceCandidateRow,
    ExperienceJobRow,
)
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.reports.persistence.tables import IncidentReportRow


def utc_now() -> datetime:
    return datetime.now(UTC)


class ExperienceStore:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._factory = session_factory
        self._clock = clock

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("经验任务时间必须包含时区")
        return value.astimezone(UTC)

    async def create(
        self,
        report_id: UUID,
        expected_report_content_hash: str,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
    ) -> tuple[JobView, bool]:
        require_permission(principal, "POST", "create_experience_job")
        now = self._now()
        if now >= principal.expires_at:
            raise OperatorAccessError("operator_unauthenticated", "操作者认证已经过期")
        async with self._factory() as session, session.begin():
            admission = await session.get(OperatorAuthorizationRow, authorization_id)
            if (
                admission is None
                or admission.subject_id != principal.subject_id
                or admission.credential_id != principal.credential_id
                or admission.role != principal.role.value
                or admission.operation != "create_experience_job"
                or admission.resources != {"report_id": str(report_id)}
            ):
                raise OperatorAccessError("operator_permission_denied", "任务缺少同源授权准入", 403)
            # 报告行锁只用于串行化同报告创建；Worker 不取得此锁，避免反向锁依赖。
            report_row = await session.scalar(
                select(IncidentReportRow).where(IncidentReportRow.id == report_id).with_for_update()
            )
            if report_row is None:
                raise ExperienceError("experience_report_not_found", "经验来源报告不存在", 404)
            source = await load_source(session, report_id)
            if source.report_content_hash != expected_report_content_hash:
                raise ExperienceError("source_conflict", "经验来源报告版本与请求不一致")
            source_hash = canonical_hash(source)
            row = await session.scalar(
                select(ExperienceJobRow)
                .where(
                    ExperienceJobRow.report_id == report_id,
                    ExperienceJobRow.policy_version == GENERATION_POLICY_VERSION,
                )
                .with_for_update(read=True)
            )
            created = row is None
            if row is None:
                row = ExperienceJobRow(
                    id=uuid4(),
                    report_id=report_id,
                    policy_version=GENERATION_POLICY_VERSION,
                    source=source.model_dump(mode="json"),
                    source_hash=source_hash,
                    requested_by_id=principal.subject_id,
                    authorization_id=authorization_id,
                    status="queued",
                    attempt_count=0,
                    max_attempts=3,
                    available_at=now,
                    lease_token=None,
                    locked_until=None,
                    last_error_code=None,
                    created_at=now,
                    updated_at=now,
                    completed_at=None,
                )
                session.add(row)
                await session.flush()
            elif row.source_hash != source_hash:
                raise ExperienceError("source_conflict", "已固定的经验来源发生漂移")
            return await _job_view(session, row), created

    async def get(self, job_id: UUID) -> JobView:
        async with self._factory() as session, session.begin():
            # 尝试和候选另表读取，持有共享锁以免与领取/结算跨语句混读。
            row = await session.scalar(
                select(ExperienceJobRow)
                .where(ExperienceJobRow.id == job_id)
                .with_for_update(read=True)
            )
            if row is None:
                raise ExperienceError("experience_job_not_found", "经验生成任务不存在", 404)
            return await _job_view(session, row)

    async def get_candidate(self, candidate_id: UUID) -> CandidateView:
        async with self._factory() as session:
            row = await session.get(ExperienceCandidateRow, candidate_id)
            if row is None:
                raise ExperienceError("experience_candidate_not_found", "经验候选不存在", 404)
            job = await session.get(ExperienceJobRow, row.job_id)
            if job is None or job.status != "succeeded":
                raise ExperienceError("source_conflict", "候选与成功任务不一致")
            return _candidate_view(row, job)

    async def claim_next(
        self,
        *,
        worker_id: str,
        lease_duration: timedelta = timedelta(seconds=30),
    ) -> ExperienceClaim | None:
        if not worker_id.strip() or len(worker_id) > 100:
            raise ValueError("经验 Worker 标识必须为 1 到 100 字符")
        if not timedelta(seconds=1) <= lease_duration <= timedelta(seconds=120):
            raise ValueError("经验任务租约必须在 1 到 120 秒之间")
        now = self._now()
        async with self._factory() as session, session.begin():
            # 一次最多收敛八个耗尽任务，避免领取事务无界扫描。
            for _ in range(8):
                row = await session.scalar(
                    select(ExperienceJobRow)
                    .where(
                        ExperienceJobRow.policy_version == GENERATION_POLICY_VERSION,
                        or_(
                            and_(
                                ExperienceJobRow.status == "queued",
                                ExperienceJobRow.available_at <= now,
                            ),
                            and_(
                                ExperienceJobRow.status == "running",
                                ExperienceJobRow.locked_until <= now,
                            ),
                        ),
                    )
                    .order_by(
                        ExperienceJobRow.available_at,
                        ExperienceJobRow.created_at,
                        ExperienceJobRow.id,
                    )
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if row is None:
                    return None
                if row.status == "running":
                    old = await session.scalar(
                        select(ExperienceAttemptRow).where(
                            ExperienceAttemptRow.job_id == row.id,
                            ExperienceAttemptRow.number == row.attempt_count,
                        )
                    )
                    if old is None or old.status != "running":
                        raise ExperienceError("source_conflict", "任务租约缺少有效尝试")
                    old.status, old.finished_at, old.error_code = (
                        "lease_expired",
                        now,
                        "lease_expired",
                    )
                row.updated_at = now
                row.lease_token, row.locked_until = None, None
                if row.attempt_count >= row.max_attempts:
                    row.status, row.completed_at = "failed", now
                    row.last_error_code = "attempts_exhausted"
                    await session.flush()
                    continue
                token, attempt_id = uuid4(), uuid4()
                expires_at = now + lease_duration
                row.status = "running"
                row.attempt_count += 1
                row.lease_token, row.locked_until = token, expires_at
                session.add(
                    ExperienceAttemptRow(
                        id=attempt_id,
                        job_id=row.id,
                        number=row.attempt_count,
                        worker_id=worker_id,
                        lease_token=token,
                        status="running",
                        error_code=None,
                        started_at=now,
                        lease_expires_at=expires_at,
                        finished_at=None,
                    )
                )
                return ExperienceClaim(row.id, attempt_id, token, expires_at)
        return None

    async def get_claim_source(self, claim: ExperienceClaim) -> ExperienceSource:
        async with self._factory() as session, session.begin():
            row, _ = await _active(session, claim, self._now())
            source = await _revalidate_source(session, row)
            _check_deadline(row, self._now())
            return source

    async def complete(self, claim: ExperienceClaim, draft: ExperienceDraft) -> CandidateView:
        async with self._factory() as session, session.begin():
            row = await session.scalar(
                select(ExperienceJobRow)
                .where(
                    ExperienceJobRow.id == claim.job_id,
                )
                .with_for_update()
            )
            if row is not None and row.status == "succeeded":
                attempt = await session.get(ExperienceAttemptRow, claim.attempt_id)
                existing = await session.scalar(
                    select(ExperienceCandidateRow).where(
                        ExperienceCandidateRow.job_id == row.id,
                    )
                )
                if (
                    attempt is not None
                    and attempt.job_id == row.id
                    and attempt.status == "succeeded"
                    and attempt.lease_token == claim.lease_token
                    and existing is not None
                    and existing.content_hash == canonical_hash(draft)
                ):
                    return _candidate_view(existing, row)
                raise ExperienceError("experience_lease_lost", "旧尝试不能结算已完成任务")
            row, attempt = await _active(session, claim, self._now())
            source = await _revalidate_source(session, row)
            expected = project_source(source)
            if draft != expected:
                raise ExperienceError("candidate_conflict", "候选与已固定的事实投影不一致")
            now = self._now()
            _check_deadline(row, now)
            candidate = ExperienceCandidateRow(
                id=_candidate_id(row.id),
                job_id=row.id,
                version=1,
                status="pending_review",
                source_hash=row.source_hash,
                content=expected.model_dump(mode="json"),
                content_hash=canonical_hash(expected),
                created_at=now,
            )
            session.add(candidate)
            row.status, row.updated_at, row.completed_at = "succeeded", now, now
            row.lease_token, row.locked_until, row.last_error_code = None, None, None
            attempt.status, attempt.finished_at = "succeeded", now
            await session.flush()
            return _candidate_view(candidate, row)

    async def fail(
        self,
        claim: ExperienceClaim,
        *,
        error_code: str,
        retryable: bool,
    ) -> None:
        async with self._factory() as session, session.begin():
            row, attempt = await _active(session, claim, self._now())
            now = self._now()
            _check_deadline(row, now)
            attempt.status, attempt.finished_at, attempt.error_code = "failed", now, error_code
            retry = retryable and row.attempt_count < row.max_attempts
            row.status = "queued" if retry else "failed"
            row.available_at = now + timedelta(seconds=2**row.attempt_count) if retry else now
            row.updated_at, row.completed_at = now, None if retry else now
            row.lease_token, row.locked_until = None, None
            row.last_error_code = error_code


def _candidate_id(job_id: UUID) -> UUID:
    return uuid5(NAMESPACE_URL, f"opsswarm:experience:{job_id}:1")


def _check_deadline(row: ExperienceJobRow, now: datetime) -> None:
    if row.locked_until is None or now >= row.locked_until:
        raise ExperienceError("experience_lease_lost", "经验任务租约已经失效")


async def _active(
    session: AsyncSession,
    claim: ExperienceClaim,
    now: datetime,
) -> tuple[ExperienceJobRow, ExperienceAttemptRow]:
    row = await session.scalar(
        select(ExperienceJobRow)
        .where(
            ExperienceJobRow.id == claim.job_id,
        )
        .with_for_update()
    )
    attempt = await session.get(ExperienceAttemptRow, claim.attempt_id)
    if (
        row is None
        or row.status != "running"
        or row.lease_token != claim.lease_token
        or attempt is None
        or attempt.job_id != row.id
        or attempt.status != "running"
        or attempt.number != row.attempt_count
        or attempt.lease_token != claim.lease_token
        or attempt.lease_expires_at != row.locked_until
    ):
        raise ExperienceError("experience_lease_lost", "经验任务不是当前有效尝试")
    _check_deadline(row, now)
    return row, attempt


def _source(row: ExperienceJobRow) -> ExperienceSource:
    try:
        source = ExperienceSource.model_validate(row.source)
        if (
            canonical_hash(source) != row.source_hash
            or source.report_id != row.report_id
            or row.policy_version != GENERATION_POLICY_VERSION
            or row.max_attempts != 3
        ):
            raise ValueError
        return source
    except ValueError:
        raise ExperienceError("source_conflict", "已持久化的经验来源无法校验") from None


async def _revalidate_source(session: AsyncSession, row: ExperienceJobRow) -> ExperienceSource:
    source = _source(row)
    current = await load_source(session, row.report_id)
    if canonical_hash(current) != row.source_hash:
        raise ExperienceError("source_conflict", "经验来源记录与冻结快照不一致")
    return source


def _candidate_view(row: ExperienceCandidateRow, job: ExperienceJobRow) -> CandidateView:
    try:
        draft = ExperienceDraft.model_validate(row.content)
        if (
            row.id != _candidate_id(job.id)
            or row.job_id != job.id
            or row.source_hash != job.source_hash
            or row.content_hash != canonical_hash(draft)
            or draft != project_source(_source(job))
        ):
            raise ValueError
        return CandidateView.model_validate(
            {
                "id": row.id,
                "job_id": row.job_id,
                "version": row.version,
                "status": row.status,
                "source_hash": row.source_hash,
                "content": draft,
                "content_hash": row.content_hash,
                "created_at": row.created_at,
            }
        )
    except ValueError:
        raise ExperienceError("candidate_conflict", "已持久化的经验候选无法校验") from None


async def _job_view(session: AsyncSession, row: ExperienceJobRow) -> JobView:
    source = _source(row)
    attempts = list(
        await session.scalars(
            select(ExperienceAttemptRow)
            .where(
                ExperienceAttemptRow.job_id == row.id,
            )
            .order_by(ExperienceAttemptRow.number)
        )
    )
    candidate = await session.scalar(
        select(ExperienceCandidateRow).where(
            ExperienceCandidateRow.job_id == row.id,
        )
    )
    if len(attempts) != row.attempt_count or (
        (candidate is not None) != (row.status == "succeeded")
    ):
        raise ExperienceError("source_conflict", "经验任务与尝试或候选事实不一致")
    if candidate is not None:
        _candidate_view(candidate, row)
    return JobView.model_validate(
        dict(
            id=row.id,
            report_id=row.report_id,
            policy_version=row.policy_version,
            source=source,
            source_hash=row.source_hash,
            requested_by_id=row.requested_by_id,
            authorization_id=row.authorization_id,
            status=row.status,
            attempt_count=row.attempt_count,
            max_attempts=row.max_attempts,
            attempts=tuple(
                AttemptView.model_validate(item, from_attributes=True) for item in attempts
            ),
            candidate_id=candidate.id if candidate is not None else None,
            last_error_code=row.last_error_code,
            created_at=row.created_at,
            updated_at=row.updated_at,
            completed_at=row.completed_at,
        )
    )
