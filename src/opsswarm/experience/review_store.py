"""可信审核用例：同源准入、唯一决定、原子确认及业务结果审计。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.experience.artifact_validation import validated_candidate
from opsswarm.experience.contracts import ExperienceError, canonical_hash
from opsswarm.experience.records import CandidateView
from opsswarm.experience.review_contracts import (
    REVIEW_POLICY_VERSION,
    ConfirmedExperienceContent,
    EntryView,
    ReviewCommand,
    ReviewDecision,
    ReviewResult,
    ReviewState,
    ReviewView,
)
from opsswarm.experience.review_policy import evaluate_review
from opsswarm.experience.review_tables import (
    ExperienceEntryRow,
    ExperienceReviewCommandRow,
    ExperienceReviewRow,
)
from opsswarm.experience.review_validation import (
    entry_id_for_candidate,
    review_id_for_candidate,
    validated_entry,
    validated_review,
)
from opsswarm.experience.sources import load_source
from opsswarm.experience.store import utc_now
from opsswarm.experience.tables import ExperienceCandidateRow, ExperienceJobRow
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.identity.tables import OperatorAuthorizationRow


@dataclass(frozen=True)
class ReviewOutcome:
    result: ReviewResult | None = None
    error: ExperienceError | None = None


@dataclass(frozen=True)
class ReviewBundle:
    candidate: CandidateView
    job: ExperienceJobRow
    review: ReviewView | None
    entry: EntryView | None


class ExperienceReviewStore:
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
            raise ValueError("审核时间必须包含时区")
        return value.astimezone(UTC)

    async def decide(
        self,
        candidate_id: UUID,
        command: ReviewCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
    ) -> ReviewOutcome:
        # 应用服务再次校验权限，不把 HTTP 依赖当作唯一边界。
        require_permission(principal, "POST", "review_experience_candidate")
        if self._now() >= principal.expires_at:
            raise OperatorAccessError("operator_unauthenticated", "操作者认证已经过期")
        async with self._factory() as session, session.begin():
            admission = await session.get(OperatorAuthorizationRow, authorization_id)
            if (
                admission is None
                or admission.subject_id != principal.subject_id
                or admission.credential_id != principal.credential_id
                or admission.role != principal.role.value
                or admission.method != "POST"
                or admission.operation != "review_experience_candidate"
                or admission.resources != {"candidate_id": str(candidate_id)}
                or "manage_report" not in admission.permissions
            ):
                raise OperatorAccessError("operator_permission_denied", "审核缺少同源授权准入", 403)
            bundle: ReviewBundle | None = None
            try:
                bundle = await _bundle(session, candidate_id, read=False)
                if self._now() >= principal.expires_at:
                    raise OperatorAccessError("operator_unauthenticated", "操作者认证已经过期")
                action = evaluate_review(
                    bundle.candidate,
                    command,
                    subject_id=principal.subject_id,
                    existing=bundle.review,
                )
                if action == "replay":
                    assert bundle.review is not None
                    result = ReviewResult(applied=False, review=bundle.review, entry=bundle.entry)
                else:
                    current = await load_source(session, bundle.job.report_id)
                    if canonical_hash(current) != bundle.candidate.source_hash:
                        raise ExperienceError("source_conflict", "审核来源与生成时冻结材料发生漂移")
                    now = self._now()
                    if now >= principal.expires_at:
                        raise OperatorAccessError("operator_unauthenticated", "操作者认证已经过期")
                    if now < bundle.candidate.created_at:
                        raise ExperienceError(
                            "experience_review_time_conflict", "审核时间早于候选生成时间"
                        )
                    async with session.begin_nested():
                        result = await _apply(
                            session, bundle.candidate, command, principal, admission, now
                        )
                _record_command(
                    session,
                    candidate_id,
                    command,
                    principal.subject_id,
                    authorization_id,
                    now=self._now(),
                    result=result,
                )
                return ReviewOutcome(result=result)
            except ExperienceError as error:
                # 业务拒绝须先提交结果；在事务外由 API 转成错误响应。
                _record_command(
                    session,
                    candidate_id,
                    command,
                    principal.subject_id,
                    authorization_id,
                    now=self._now(),
                    error=error,
                    review_id=bundle.review.id if bundle is not None and bundle.review else None,
                )
                return ReviewOutcome(error=error)

    async def get_state(self, candidate_id: UUID) -> ReviewState:
        async with self._factory() as session, session.begin():
            bundle = await _bundle(session, candidate_id, read=True)
            return ReviewState(
                candidate_id=candidate_id,
                candidate_version=bundle.candidate.version,
                candidate_content_hash=bundle.candidate.content_hash,
                review_version=0 if bundle.review is None else 1,
                status=(
                    "pending_review"
                    if bundle.review is None
                    else "confirmed"
                    if bundle.review.decision is ReviewDecision.CONFIRM
                    else "rejected"
                ),
                review=bundle.review,
                entry_id=bundle.entry.id if bundle.entry else None,
            )

    async def get_entry(self, entry_id: UUID) -> EntryView:
        async with self._factory() as session, session.begin():
            return await load_confirmed_entry_in_session(session, entry_id)


async def load_confirmed_entry_in_session(session: AsyncSession, entry_id: UUID) -> EntryView:
    """复用调用方事务复核历史确认工件，不跨仓储开启独立读取快照。"""
    row = await session.get(ExperienceEntryRow, entry_id)
    if row is None:
        raise ExperienceError("experience_entry_not_found", "已确认经验版本不存在", 404)
    bundle = await _bundle(session, row.candidate_id, read=True)
    if bundle.entry is None or bundle.entry.id != entry_id:
        raise ExperienceError("experience_entry_integrity_conflict", "确认版本与审核投影不一致")
    return bundle.entry


async def _bundle(session: AsyncSession, candidate_id: UUID, *, read: bool) -> ReviewBundle:
    candidate_row = await session.scalar(
        select(ExperienceCandidateRow)
        .where(ExperienceCandidateRow.id == candidate_id)
        .with_for_update(read=read)
    )
    if candidate_row is None:
        raise ExperienceError("experience_candidate_not_found", "经验候选不存在", 404)
    job = await session.get(ExperienceJobRow, candidate_row.job_id)
    if job is None or job.status != "succeeded":
        raise ExperienceError("source_conflict", "审核候选没有成功的生成任务")
    candidate = validated_candidate(candidate_row, job)
    review_row = await session.scalar(
        select(ExperienceReviewRow).where(
            ExperienceReviewRow.candidate_id == candidate_id,
        )
    )
    entry_row = await session.scalar(
        select(ExperienceEntryRow).where(
            ExperienceEntryRow.candidate_id == candidate_id,
        )
    )
    if review_row is None:
        if entry_row is not None:
            raise ExperienceError("experience_entry_integrity_conflict", "确认版本缺少审核决定")
        return ReviewBundle(candidate, job, None, None)
    admission = await session.get(OperatorAuthorizationRow, review_row.authorization_id)
    review = validated_review(review_row, candidate, admission)
    if (entry_row is not None) != (review.decision is ReviewDecision.CONFIRM):
        raise ExperienceError("experience_entry_integrity_conflict", "审核决定与确认版本不一致")
    entry = validated_entry(entry_row, candidate, review) if entry_row is not None else None
    return ReviewBundle(candidate, job, review, entry)


async def _apply(
    session: AsyncSession,
    candidate: CandidateView,
    command: ReviewCommand,
    principal: OperatorPrincipal,
    admission: OperatorAuthorizationRow,
    now: datetime,
) -> ReviewResult:
    review_row = ExperienceReviewRow(
        id=review_id_for_candidate(candidate.id),
        candidate_id=candidate.id,
        candidate_version=candidate.version,
        candidate_content_hash=candidate.content_hash,
        source_hash=candidate.source_hash,
        review_version=1,
        policy_version=REVIEW_POLICY_VERSION,
        decision=command.decision.value,
        reviewed_by_id=principal.subject_id,
        authorization_id=admission.id,
        idempotency_key=command.idempotency_key,
        command_hash=canonical_hash(command),
        reason=command.reason,
        reviewed_at=now,
    )
    session.add(review_row)
    await session.flush()
    review = validated_review(review_row, candidate, admission)
    entry: EntryView | None = None
    if command.decision is ReviewDecision.CONFIRM:
        content = ConfirmedExperienceContent(candidate_snapshot=candidate.content)
        entry_row = ExperienceEntryRow(
            id=entry_id_for_candidate(candidate.id),
            review_id=review.id,
            candidate_id=candidate.id,
            candidate_version=candidate.version,
            candidate_content_hash=candidate.content_hash,
            source_hash=candidate.source_hash,
            review_decision="confirm",
            version=1,
            status="confirmed",
            content=content.model_dump(mode="json"),
            content_hash=canonical_hash(content),
            created_at=now,
        )
        session.add(entry_row)
        await session.flush()
        entry = validated_entry(entry_row, candidate, review)
    return ReviewResult(applied=True, review=review, entry=entry)


def _record_command(
    session: AsyncSession,
    candidate_id: UUID,
    command: ReviewCommand,
    subject_id: str,
    authorization_id: UUID,
    *,
    now: datetime,
    result: ReviewResult | None = None,
    error: ExperienceError | None = None,
    review_id: UUID | None = None,
) -> None:
    session.add(
        ExperienceReviewCommandRow(
            id=uuid4(),
            authorization_id=authorization_id,
            candidate_id=candidate_id,
            requested_by_id=subject_id,
            command=command.model_dump(mode="json"),
            command_hash=canonical_hash(command),
            outcome="error" if error else "applied" if result and result.applied else "replayed",
            error_code=error.code if error else None,
            http_status=error.status_code if error else 201 if result and result.applied else 200,
            review_id=result.review.id if result else review_id,
            created_at=now,
        )
    )
