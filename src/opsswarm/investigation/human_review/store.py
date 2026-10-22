"""可信重评命令的精确幂等、事务审计与历史读取。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_prepare.contracts import PreparationError
from opsswarm.investigation.human_review.authorization import (
    OPERATION,
    check_principal,
    validate_admission,
)
from opsswarm.investigation.human_review.contracts import (
    POLICY_VERSION,
    HumanReviewAuditPage,
    HumanReviewError,
    HumanReviewJobView,
    HumanReviewSubmissionResult,
    ReassessCommand,
)
from opsswarm.investigation.human_review.queries import (
    matches_command,
    preparation_source,
    read_job,
    validated_audit,
)
from opsswarm.investigation.human_review.tables import (
    HumanReviewCommandRow,
    HumanReviewJobRow,
)
from opsswarm.investigation.human_routing.contracts import HumanRoutingView
from opsswarm.investigation.human_routing.store import HumanRoutingStore
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.human_wait.state import load_wait_state, lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.tasks.errors import TaskFactConflict


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class HumanReviewSubmission:
    result: HumanReviewSubmissionResult | None = None
    error: HumanReviewError | None = None


class HumanReviewStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock

    def _check(self, principal: OperatorPrincipal) -> datetime:
        now = normalized_now(self._clock)
        check_principal(principal, now)
        return now

    async def create(
        self,
        run_id: UUID,
        preparation_id: UUID,
        command: ReassessCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> HumanReviewSubmission:
        require_permission(principal, "POST", OPERATION)
        self._check(principal)
        if len(raw_body) > 65536:
            raise OperatorAccessError("operator_command_too_large", "重评命令超过 64 KiB", 413)
        try:
            raw = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "重评命令必须为 UTF-8", 422
            ) from None
        async with self._factory() as session, session.begin():
            admission = await session.get(
                OperatorAuthorizationRow, authorization_id, with_for_update=True
            )
            now = self._check(principal)
            try:
                validate_admission(
                    admission,
                    run_id=run_id,
                    preparation_id=preparation_id,
                    subject_id=principal.subject_id,
                    command=command,
                    raw_body=raw,
                    occurred_at=now,
                )
                if admission is None or admission.credential_id != principal.credential_id:
                    raise ValueError("凭据不一致")
            except ValueError:
                raise OperatorAccessError(
                    "operator_permission_denied", "重评命令缺少同源可信准入", 403
                ) from None
            prior = await session.scalar(
                select(HumanReviewCommandRow).where(
                    HumanReviewCommandRow.authorization_id == authorization_id
                )
            )
            if prior is not None:
                audit = await validated_audit(session, prior)
                if (
                    audit.preparation_job_id != preparation_id
                    or audit.investigation_run_id != run_id
                    or audit.command_sha256 != content_hash(command)
                ):
                    raise OperatorAccessError(
                        "operator_permission_denied", "准入已绑定另一重评命令", 403
                    )
                if audit.outcome == "error":
                    self._check(principal)
                    return HumanReviewSubmission(
                        error=HumanReviewError(
                            audit.error_code or "human_review_conflict",
                            audit.error_message or "重评冲突",
                            audit.http_status,
                        )
                    )
                run = await lock_wait_run(session, run_id, read=True)
                job = await session.get(HumanReviewJobRow, audit.job_id)
                assert job is not None
                view = await read_job(session, job, run, clock=self._clock)
                self._check(principal)
                return HumanReviewSubmission(
                    result=HumanReviewSubmissionResult(applied=False, job=view)
                )
            try:
                run = await lock_wait_run(session, run_id, read=False)
                self._check(principal)
                job = await session.scalar(
                    select(HumanReviewJobRow).where(
                        HumanReviewJobRow.investigation_run_id == run_id,
                        HumanReviewJobRow.requested_by_id == principal.subject_id,
                        HumanReviewJobRow.idempotency_key == command.idempotency_key,
                    )
                )
                applied = job is None
                if job is not None:
                    if not matches_command(job, preparation_id, command):
                        raise HumanReviewError(
                            "human_review_idempotency_conflict", "幂等键已绑定另一完整重评命令"
                        )
                    view = await read_job(session, job, run, clock=self._clock)
                else:
                    handoff, wait = await load_wait_state(session, run_id)
                    if (
                        command.handoff_id != handoff.id
                        or command.source_sha256 != handoff.source_sha256
                    ):
                        raise HumanReviewError(
                            "human_review_source_conflict", "重评命令不匹配精确交接来源"
                        )
                    if wait.status != "waiting" or run.status != "await_human":
                        raise HumanReviewError(
                            "human_review_not_available", "本次等待已取消或不再可重评"
                        )
                    if wait.state_version != command.expected_wait_version:
                        raise HumanReviewError(
                            "human_review_version_conflict", "重评所需等待版本已变化"
                        )
                    if not wait.events:
                        raise HumanReviewError(
                            "human_review_input_required", "至少补充一项人工材料后才能重评"
                        )
                    occupied = await session.scalar(
                        select(HumanReviewJobRow.id).where(
                            HumanReviewJobRow.investigation_run_id == run_id,
                            HumanReviewJobRow.preparation_job_id == preparation_id,
                        )
                    )
                    if occupied is not None:
                        raise HumanReviewError(
                            "human_review_already_exists", "当前等待版本已创建重评任务"
                        )
                    incident = await session.scalar(
                        select(IncidentRow)
                        .where(IncidentRow.id == run.incident_id)
                        .with_for_update()
                    )
                    if incident is None or incident.status != "investigating":
                        raise HumanReviewError("human_review_not_available", "事故不再允许重评")
                    source, artifact, availability = await preparation_source(
                        session, run, preparation_id, clock=self._clock
                    )
                    if content_hash(artifact) != command.preparation_sha256:
                        raise HumanReviewError(
                            "human_review_preparation_conflict", "命令不匹配完整重评工件"
                        )
                    if availability != "ready":
                        raise HumanReviewError(
                            "human_review_not_ready", "重评工件当前不可调用或原预算不足"
                        )
                    now = self._check(principal)
                    job = HumanReviewJobRow(
                        id=uuid5(preparation_id, "human-review:v1"),
                        investigation_run_id=run_id,
                        preparation_job_id=preparation_id,
                        preparation_sha256=command.preparation_sha256,
                        wait_version=wait.state_version,
                        policy_version=POLICY_VERSION,
                        requested_by_id=principal.subject_id,
                        authorization_id=authorization_id,
                        idempotency_key=command.idempotency_key,
                        command_sha256=content_hash(command),
                        status="queued",
                        attempt_count=0,
                        available_at=now,
                        locked_by=None,
                        lease_token=None,
                        locked_until=None,
                        last_error_code=None,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                    )
                    session.add(job)
                    await session.flush()
            except (HumanReviewError, PreparationError, HumanWaitError, TaskFactConflict) as error:
                failure = (
                    error
                    if isinstance(error, HumanReviewError)
                    else HumanReviewError(
                        "human_review_run_not_found"
                        if isinstance(error, HumanWaitError) and error.status_code == 404
                        else "human_review_source_conflict",
                        str(error),
                        error.status_code if isinstance(error, HumanWaitError) else 409,
                    )
                )
                _record(
                    session,
                    run_id,
                    preparation_id,
                    principal.subject_id,
                    command,
                    raw,
                    authorization_id,
                    now=self._check(principal),
                    error=failure,
                )
                await session.flush()
                self._check(principal)
                return HumanReviewSubmission(error=failure)
            assert job is not None
            _record(
                session,
                run_id,
                preparation_id,
                principal.subject_id,
                command,
                raw,
                authorization_id,
                now=now if applied else self._check(principal),
                job_id=job.id,
                applied=applied,
            )
            await session.flush()
            if applied:
                view = await read_job(session, job, run, clock=self._clock)
            self._check(principal)
            return HumanReviewSubmission(
                result=HumanReviewSubmissionResult(applied=applied, job=view)
            )

    async def get(self, run_id: UUID, preparation_id: UUID, job_id: UUID) -> HumanReviewJobView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            job = await session.scalar(
                select(HumanReviewJobRow)
                .where(
                    HumanReviewJobRow.id == job_id,
                    HumanReviewJobRow.preparation_job_id == preparation_id,
                    HumanReviewJobRow.investigation_run_id == run_id,
                )
                .with_for_update(read=True)
            )
            if job is None:
                raise HumanReviewError("human_review_job_not_found", "重评任务不存在", 404)
            return await read_job(session, job, run, clock=self._clock)

    async def get_route(self, run_id: UUID, preparation_id: UUID, job_id: UUID) -> HumanRoutingView:
        return await HumanRoutingStore(self._factory, clock=self._clock).get(
            run_id, preparation_id, job_id
        )

    async def list_commands(
        self, run_id: UUID, preparation_id: UUID, *, after_cursor: int = 0, limit: int = 50
    ) -> HumanReviewAuditPage:
        if after_cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("重评审计分页参数不合法")
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            rows = list(
                await session.scalars(
                    select(HumanReviewCommandRow)
                    .where(
                        HumanReviewCommandRow.investigation_run_id == run_id,
                        HumanReviewCommandRow.preparation_job_id == preparation_id,
                        HumanReviewCommandRow.cursor > after_cursor,
                    )
                    .order_by(HumanReviewCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            records = tuple([await validated_audit(session, row) for row in rows[:limit]])
            for job_id in {item.job_id for item in records if item.job_id is not None}:
                job = await session.get(HumanReviewJobRow, job_id)
                assert job is not None
                await read_job(session, job, run, clock=self._clock)
            return HumanReviewAuditPage(
                items=records, next_cursor=records[-1].cursor if len(rows) > limit else None
            )


def _record(
    session: AsyncSession,
    run_id: UUID,
    preparation_id: UUID,
    subject_id: str,
    command: ReassessCommand,
    raw_body: str,
    authorization_id: UUID,
    *,
    now: datetime,
    job_id: UUID | None = None,
    applied: bool = False,
    error: HumanReviewError | None = None,
) -> None:
    session.add(
        HumanReviewCommandRow(
            authorization_id=authorization_id,
            investigation_run_id=run_id,
            preparation_job_id=preparation_id,
            subject_id=subject_id,
            command=command.model_dump(mode="json"),
            raw_body=raw_body,
            command_sha256=content_hash(command),
            outcome="error" if error else "applied" if applied else "replayed",
            http_status=error.status_code if error else 201 if applied else 200,
            job_id=job_id,
            error_code=error.code if error else None,
            error_message=str(error) if error else None,
            created_at=now,
        )
    )
