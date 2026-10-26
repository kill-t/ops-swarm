"""可信发布请求的原子准备、只读观测、重放和查询。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval.conditions.sources import source_availability
from opsswarm.approval.decision.authorization import check_principal
from opsswarm.execution.release.authorization import OPERATION, validate_admission
from opsswarm.execution.release.contracts import (
    POLICY_VERSION,
    ReleaseRequestAuditPage,
    ReleaseRequestCommand,
    ReleaseRequestError,
    ReleaseRequestResult,
    ReleaseRequestView,
)
from opsswarm.execution.release.observer import FaultLabReleaseObserver, ReleaseObserver, utc_now
from opsswarm.execution.release.policy import (
    build_content,
    expires_at,
    validate_command,
    validate_release_plan,
)
from opsswarm.execution.release.queries import (
    ReleaseContext,
    load_context,
    matches_command,
    operation_id,
    read_record,
    request_id,
    validated_audit,
    view_for,
)
from opsswarm.execution.release.tables import CandidateReleaseCommandRow, CandidateReleaseRequestRow
from opsswarm.identity.models import (
    OperatorAccessError,
    OperatorPrincipal,
    OperatorRole,
    require_permission,
)
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict


@dataclass(frozen=True)
class ReleaseRequestSubmission:
    result: ReleaseRequestResult | None = None
    error: ReleaseRequestError | None = None


class CandidateReleaseRequestStore:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        observer: ReleaseObserver | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._factory, self._clock = factory, clock
        self._observer = observer if observer is not None else FaultLabReleaseObserver(clock=clock)

    def _check(self, principal: OperatorPrincipal) -> datetime:
        now = normalized_now(self._clock)
        check_principal(principal, now)
        if principal.role is not OperatorRole.EXECUTOR:
            raise OperatorAccessError(
                "operator_permission_denied", "发布执行准备仅允许可信执行者", 403
            )
        return now

    async def _current(
        self,
        session: AsyncSession,
        run: InvestigationRunRow,
        context: ReleaseContext,
        command: ReleaseRequestCommand,
        principal: OperatorPrincipal,
    ) -> datetime:
        availability = await source_availability(session, run, context.basis, clock=self._clock)
        if availability == "recorded":
            raise ReleaseRequestError("candidate_release_source_invalid", "核验来源适用性无效")
        now = self._check(principal)
        validate_command(
            command,
            context.approval,
            availability=availability,
            now=now,
            expires_at=context.basis.verification.expires_at,
        )
        validate_release_plan(context.basis.original.plan)
        return now

    async def create(
        self,
        run_id: UUID,
        job_id: UUID,
        version: int,
        command: ReleaseRequestCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> ReleaseRequestSubmission:
        require_permission(principal, "POST", OPERATION)
        self._check(principal)
        if len(raw_body) > 65536:
            raise OperatorAccessError("operator_command_too_large", "发布准备命令超过64 KiB", 413)
        try:
            raw = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "发布准备命令必须为UTF-8", 422
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
                    job_id=job_id,
                    version=version,
                    subject_id=principal.subject_id,
                    command=command,
                    raw_body=raw,
                    occurred_at=now,
                )
                if admission is None or admission.credential_id != principal.credential_id:
                    raise ValueError("凭据不一致")
            except ValueError:
                raise OperatorAccessError(
                    "operator_permission_denied", "发布准备缺少同源可信准入", 403
                ) from None
            prior = await session.scalar(
                select(CandidateReleaseCommandRow).where(
                    CandidateReleaseCommandRow.authorization_id == authorization_id
                )
            )
            if prior is not None:
                audit = await validated_audit(session, prior)
                if (
                    audit.investigation_run_id != run_id
                    or audit.candidate_job_id != job_id
                    or audit.verification_version != version
                    or audit.command != command
                ):
                    raise OperatorAccessError(
                        "operator_permission_denied", "准入已绑定另一命令", 403
                    )
                if audit.outcome == "error":
                    self._check(principal)
                    return ReleaseRequestSubmission(
                        error=ReleaseRequestError(
                            audit.error_code or "candidate_release_conflict",
                            audit.error_message or "发布准备冲突",
                            audit.http_status,
                        )
                    )
            rollback_only = prior is not None
            try:
                run = await lock_wait_run(session, run_id, read=False)
                await session.scalar(
                    select(CandidateJobRow)
                    .where(
                        CandidateJobRow.id == job_id, CandidateJobRow.investigation_run_id == run_id
                    )
                    .with_for_update()
                )
                await session.scalar(
                    select(IncidentRow).where(IncidentRow.id == run.incident_id).with_for_update()
                )
                self._check(principal)
                row = await session.scalar(
                    select(CandidateReleaseRequestRow).where(
                        CandidateReleaseRequestRow.investigation_run_id == run_id,
                        CandidateReleaseRequestRow.requested_by_id == principal.subject_id,
                        CandidateReleaseRequestRow.idempotency_key == command.idempotency_key,
                    )
                )
                if row is not None and not matches_command(row, job_id, version, command):
                    raise ReleaseRequestError(
                        "candidate_release_idempotency_conflict", "幂等键已绑定另一完整命令"
                    )
                rollback_only = rollback_only or row is not None
                has_target = (
                    await session.scalar(
                        select(CandidateReleaseRequestRow.id).where(
                            CandidateReleaseRequestRow.investigation_run_id == run_id,
                            CandidateReleaseRequestRow.candidate_job_id == job_id,
                            CandidateReleaseRequestRow.verification_version == version,
                        )
                    )
                    is not None
                )
                try:
                    context = await load_context(session, run, job_id, version, clock=self._clock)
                    existing = await session.scalar(
                        select(CandidateReleaseRequestRow).where(
                            CandidateReleaseRequestRow.approval_decision_id == context.approval.id
                        )
                    )
                    if existing is not None:
                        await read_record(session, existing, context)
                except (ValueError, CallBudgetExceeded, TaskFactConflict):
                    rollback_only = rollback_only or has_target
                    raise
                applied = row is None
                if row is None:
                    if prior is not None:
                        raise ReleaseRequestError(
                            "candidate_release_integrity_violation", "原成功命令缺少请求"
                        )
                    if existing is not None:
                        raise ReleaseRequestError(
                            "candidate_release_request_exists", "该精确批准已有不可变发布请求"
                        )
                    started = await self._current(session, run, context, command, principal)
                    observation = await self._observer.observe()
                    now = await self._current(session, run, context, command, principal)
                    if observation.observed_at < started:
                        raise ReleaseRequestError(
                            "candidate_release_observation_stale", "观测不属于本次准备"
                        )
                    deadline = expires_at(context.basis.verification.expires_at, observation, now)
                    operation = operation_id(context.approval.id)
                    content = build_content(
                        context.approval, context.basis.original.plan, observation, operation
                    )
                    row = CandidateReleaseRequestRow(
                        id=request_id(context.approval.id),
                        operation_id=operation,
                        investigation_run_id=run_id,
                        candidate_job_id=job_id,
                        approval_decision_id=context.approval.id,
                        approval_content_sha256=context.approval.content_sha256,
                        verification_version=version,
                        incident_version=context.approval.incident_version,
                        status="prepared",
                        policy_version=POLICY_VERSION,
                        requested_by_id=principal.subject_id,
                        authorization_id=authorization_id,
                        idempotency_key=command.idempotency_key,
                        command_sha256=content_hash(command),
                        content=content.model_dump(mode="json"),
                        content_sha256=content_hash(content),
                        created_at=now,
                        expires_at=deadline,
                    )
                    rollback_only = True
                    session.add(row)
                    await session.flush()
                if prior is None:
                    _record(
                        session,
                        run_id,
                        job_id,
                        version,
                        principal.subject_id,
                        command,
                        raw,
                        authorization_id,
                        now=row.created_at if applied else self._check(principal),
                        request_identifier=row.id,
                        applied=applied,
                    )
                    await session.flush()
                record = await read_record(session, row, context)
                view = await view_for(session, run, context, record, clock=self._clock)
                if applied:
                    now = await self._current(session, run, context, command, principal)
                    if now >= record.expires_at:
                        raise ReleaseRequestError("candidate_release_expired", "提交前准备期限已到")
                self._check(principal)
                return ReleaseRequestSubmission(
                    result=ReleaseRequestResult(applied=applied, request=view)
                )
            except OperatorAccessError:
                raise
            except (ValueError, CallBudgetExceeded, TaskFactConflict) as error:
                failure = (
                    error
                    if isinstance(error, ReleaseRequestError)
                    else ReleaseRequestError(
                        "candidate_release_source_invalid",
                        "发布准备来源或原预算无法完整校验",
                        404 if getattr(error, "status_code", None) == 404 else 409,
                    )
                )
                if rollback_only:
                    if failure is error:
                        raise
                    raise failure from error
                _record(
                    session,
                    run_id,
                    job_id,
                    version,
                    principal.subject_id,
                    command,
                    raw,
                    authorization_id,
                    now=self._check(principal),
                    error=failure,
                )
                await session.flush()
                self._check(principal)
                return ReleaseRequestSubmission(error=failure)

    async def get(self, run_id: UUID, job_id: UUID, version: int) -> ReleaseRequestView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            context = await load_context(session, run, job_id, version, clock=self._clock)
            row = await session.scalar(
                select(CandidateReleaseRequestRow).where(
                    CandidateReleaseRequestRow.approval_decision_id == context.approval.id
                )
            )
            record = None if row is None else await read_record(session, row, context)
            return await view_for(session, run, context, record, clock=self._clock)

    async def list_commands(
        self, run_id: UUID, job_id: UUID, version: int, *, after_cursor: int = 0, limit: int = 50
    ) -> ReleaseRequestAuditPage:
        if after_cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("发布准备审计分页参数无效")
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            rows = list(
                await session.scalars(
                    select(CandidateReleaseCommandRow)
                    .where(
                        CandidateReleaseCommandRow.investigation_run_id == run_id,
                        CandidateReleaseCommandRow.candidate_job_id == job_id,
                        CandidateReleaseCommandRow.verification_version == version,
                        CandidateReleaseCommandRow.cursor > after_cursor,
                    )
                    .order_by(CandidateReleaseCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            records = tuple([await validated_audit(session, row) for row in rows[:limit]])
            if any(record.request_id is not None for record in records):
                context = await load_context(session, run, job_id, version, clock=self._clock)
                for record in records:
                    if record.request_id is not None:
                        row = await session.get(CandidateReleaseRequestRow, record.request_id)
                        if row is None:
                            raise ReleaseRequestError(
                                "candidate_release_integrity_violation", "审计缺少原请求"
                            )
                        await read_record(session, row, context)
            return ReleaseRequestAuditPage(
                items=records, next_cursor=records[-1].cursor if len(rows) > limit else None
            )


def _record(
    session: AsyncSession,
    run_id: UUID,
    job_id: UUID,
    version: int,
    subject_id: str,
    command: ReleaseRequestCommand,
    raw: str,
    authorization_id: UUID,
    *,
    now: datetime,
    request_identifier: UUID | None = None,
    applied: bool = False,
    error: ReleaseRequestError | None = None,
) -> None:
    session.add(
        CandidateReleaseCommandRow(
            authorization_id=authorization_id,
            investigation_run_id=run_id,
            candidate_job_id=job_id,
            verification_version=version,
            subject_id=subject_id,
            command=command.model_dump(mode="json"),
            raw_body=raw,
            command_sha256=content_hash(command),
            outcome="error" if error else "applied" if applied else "replayed",
            http_status=error.status_code if error else 201 if applied else 200,
            request_id=request_identifier,
            error_code=error.code if error else None,
            error_message=str(error) if error else None,
            created_at=now,
        )
    )
