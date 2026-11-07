"""可信执行任务的原子创建、完整重放及业务审计查询。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.execution.release.observer import utc_now
from opsswarm.execution.release_jobs.authorization import (
    binding,
    principal_credential,
    validate_admission,
)
from opsswarm.execution.release_jobs.contracts import (
    OPERATION,
    POLICY_VERSION,
    ExecutionAuditPage,
    ExecutionCommand,
    ExecutionJobError,
    ExecutionJobView,
    ExecutionSubmission,
)
from opsswarm.execution.release_jobs.policy import dispatch_command, execution_id
from opsswarm.execution.release_jobs.queries import matches, read_audit, read_view
from opsswarm.execution.release_jobs.source import load_request, require_current
from opsswarm.execution.release_jobs.tables import (
    ReleaseExecutionCommandRow,
    ReleaseExecutionJobRow,
)
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.identity.settings import OperatorAuthSettings, OperatorCredential
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run, read_wait_run_snapshot
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow


@dataclass(frozen=True)
class ExecutionCreateOutcome:
    submission: ExecutionSubmission | None = None
    error: ExecutionJobError | None = None


async def lock_parent(
    session: AsyncSession, run_id: UUID, candidate_job_id: UUID, *, read: bool = False
) -> InvestigationRunRow:
    run = await lock_wait_run(session, run_id, read=read)
    await session.scalar(
        select(CandidateJobRow)
        .where(
            CandidateJobRow.id == candidate_job_id, CandidateJobRow.investigation_run_id == run_id
        )
        .with_for_update(read=read)
    )
    await session.scalar(
        select(IncidentRow).where(IncidentRow.id == run.incident_id).with_for_update(read=read)
    )
    return run


class ReleaseExecutionStore:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        auth_provider: Callable[[], OperatorAuthSettings] = OperatorAuthSettings,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._factory, self._auth_provider, self._clock = factory, auth_provider, clock

    def _credential(self, principal: OperatorPrincipal) -> OperatorCredential:
        return principal_credential(self._auth_provider(), principal, normalized_now(self._clock))

    async def create(
        self,
        run_id: UUID,
        job_id: UUID,
        version: int,
        command: ExecutionCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> ExecutionCreateOutcome:
        require_permission(principal, "POST", OPERATION)
        self._credential(principal)
        if not 2 <= len(raw_body) <= 65536:
            raise OperatorAccessError(
                "operator_command_too_large", "执行任务命令超过64KiB或为空", 413
            )
        try:
            raw = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "执行命令须为UTF-8", 422
            ) from None
        async with self._factory() as session, session.begin():
            admission = await session.get(
                OperatorAuthorizationRow, authorization_id, with_for_update=True
            )
            self._credential(principal)
            validate_admission(
                admission,
                run_id=run_id,
                job_id=job_id,
                version=version,
                subject_id=principal.subject_id,
                command=command,
                raw_body=raw,
                occurred_at=normalized_now(self._clock),
            )
            if admission is None or admission.credential_id != principal.credential_id:
                raise OperatorAccessError("operator_permission_denied", "执行任务缺少同源凭据", 403)
            prior = await session.scalar(
                select(ReleaseExecutionCommandRow).where(
                    ReleaseExecutionCommandRow.authorization_id == authorization_id
                )
            )
            if prior is not None:
                audit = await read_audit(session, prior)
                if audit.command != command or (
                    audit.investigation_run_id,
                    audit.candidate_job_id,
                    audit.verification_version,
                ) != (run_id, job_id, version):
                    raise OperatorAccessError(
                        "operator_permission_denied", "准入已绑定另一执行命令", 403
                    )
                if audit.outcome == "error":
                    return ExecutionCreateOutcome(
                        error=ExecutionJobError(
                            audit.error_code or "release_execution_conflict",
                            audit.error_message or "执行任务冲突",
                            audit.http_status,
                        )
                    )
            has_target = (
                await session.scalar(
                    select(ReleaseExecutionJobRow.id).where(
                        ReleaseExecutionJobRow.investigation_run_id == run_id,
                        ReleaseExecutionJobRow.candidate_job_id == job_id,
                        ReleaseExecutionJobRow.verification_version == version,
                    )
                )
                is not None
            )
            rollback_only = prior is not None
            try:
                run = await lock_parent(session, run_id, job_id)
                try:
                    context, request = await load_request(
                        session, run, job_id, version, clock=self._clock
                    )
                    if request is None:
                        raise ExecutionJobError(
                            "release_execution_request_not_found", "发布准备请求不存在", 404
                        )
                    existing = await session.scalar(
                        select(ReleaseExecutionJobRow).where(
                            ReleaseExecutionJobRow.request_id == request.id
                        )
                    )
                    if existing is not None:
                        await read_view(session, existing, request)
                except ValueError:
                    rollback_only = rollback_only or has_target
                    raise
                same = await session.scalar(
                    select(ReleaseExecutionJobRow).where(
                        ReleaseExecutionJobRow.investigation_run_id == run_id,
                        ReleaseExecutionJobRow.requested_by_id == principal.subject_id,
                        ReleaseExecutionJobRow.idempotency_key == command.idempotency_key,
                    )
                )
                if same is not None and (existing is None or same.id != existing.id):
                    try:
                        _, original = await load_request(
                            session,
                            run,
                            same.candidate_job_id,
                            same.verification_version,
                            clock=self._clock,
                        )
                        if original is None:
                            raise ExecutionJobError(
                                "release_execution_integrity_violation", "幂等键原任务缺少准备来源"
                            )
                        await read_view(session, same, original)
                    except ValueError:
                        rollback_only = True
                        raise
                if same is not None and not matches(same, job_id, version, command):
                    raise ExecutionJobError(
                        "release_execution_idempotency_conflict", "幂等键已绑定另一执行命令"
                    )
                if (
                    command.request_id,
                    command.request_content_sha256,
                    command.expected_incident_version,
                ) != (request.id, request.content_sha256, request.incident_version):
                    raise ExecutionJobError(
                        "release_execution_request_mismatch", "执行命令未精确绑定原准备请求"
                    )
                applied = same is None
                if same is None:
                    if existing is not None:
                        raise ExecutionJobError(
                            "release_execution_job_exists", "该准备请求已有唯一执行任务"
                        )
                    await require_current(session, run, context, request, clock=self._clock)
                    credential = self._credential(principal)
                    now = normalized_now(self._clock)
                    remote = dispatch_command(
                        request, created_at=now, credential_expires_at=credential.expires_at
                    )
                    same = ReleaseExecutionJobRow(
                        id=execution_id(request.id),
                        investigation_run_id=run_id,
                        candidate_job_id=job_id,
                        verification_version=version,
                        request_id=request.id,
                        request_content_sha256=request.content_sha256,
                        operation_id=request.operation_id,
                        policy_version=POLICY_VERSION,
                        requested_by_id=principal.subject_id,
                        credential_id=principal.credential_id,
                        credential_binding_sha256=binding(credential),
                        credential_expires_at=credential.expires_at,
                        authorization_id=authorization_id,
                        idempotency_key=command.idempotency_key,
                        command_sha256=content_hash(command),
                        dispatch_command=remote.model_dump(mode="json"),
                        dispatch_sha256=content_hash(remote),
                        status="queued",
                        phase="prepared",
                        attempt_count=0,
                        locked_by=None,
                        lease_token=None,
                        locked_until=None,
                        dispatched_at=None,
                        dispatch_attempt_id=None,
                        created_at=now,
                        updated_at=now,
                        completed_at=None,
                        last_error_code=None,
                    )
                    session.add(same)
                    rollback_only = True
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
                        now=same.created_at if applied else normalized_now(self._clock),
                        identifier=same.id,
                        applied=applied,
                    )
                    await session.flush()
                view = await read_view(session, same, request)
                if applied:
                    await require_current(session, run, context, request, clock=self._clock)
                self._credential(principal)
                return ExecutionCreateOutcome(
                    submission=ExecutionSubmission(applied=applied, job=view)
                )
            except OperatorAccessError:
                raise
            except ValueError as error:
                failure = (
                    error
                    if isinstance(error, ExecutionJobError)
                    else ExecutionJobError(
                        "release_execution_source_invalid",
                        "执行任务来源无法完整校验",
                        404 if getattr(error, "status_code", None) == 404 else 409,
                    )
                )
                if rollback_only:
                    if failure is error:
                        raise
                    raise failure from error
                self._credential(principal)
                _record(
                    session,
                    run_id,
                    job_id,
                    version,
                    principal.subject_id,
                    command,
                    raw,
                    authorization_id,
                    now=normalized_now(self._clock),
                    error=failure,
                )
                await session.flush()
                self._credential(principal)
                return ExecutionCreateOutcome(error=failure)

    async def get(self, run_id: UUID, job_id: UUID, version: int) -> ExecutionJobView:
        async with self._factory() as session, session.begin():
            run = await read_wait_run_snapshot(session, run_id)
            _, request = await load_request(session, run, job_id, version, clock=self._clock)
            if request is None:
                return ExecutionJobView(record=None, attempts=(), result=None)
            row = await session.scalar(
                select(ReleaseExecutionJobRow).where(
                    ReleaseExecutionJobRow.request_id == request.id
                )
            )
            return (
                ExecutionJobView(record=None, attempts=(), result=None)
                if row is None
                else await read_view(session, row, request)
            )

    async def list_commands(
        self, run_id: UUID, job_id: UUID, version: int, *, after_cursor: int = 0, limit: int = 50
    ) -> ExecutionAuditPage:
        if after_cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("执行审计分页参数无效")
        async with self._factory() as session, session.begin():
            run = await lock_parent(session, run_id, job_id, read=True)
            rows = list(
                await session.scalars(
                    select(ReleaseExecutionCommandRow)
                    .where(
                        ReleaseExecutionCommandRow.investigation_run_id == run_id,
                        ReleaseExecutionCommandRow.candidate_job_id == job_id,
                        ReleaseExecutionCommandRow.verification_version == version,
                        ReleaseExecutionCommandRow.cursor > after_cursor,
                    )
                    .order_by(ReleaseExecutionCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            audits = tuple([await read_audit(session, row) for row in rows[:limit]])
            if any(a.execution_job_id is not None for a in audits):
                _, request = await load_request(session, run, job_id, version, clock=self._clock)
                if request is None:
                    raise ExecutionJobError(
                        "release_execution_integrity_violation", "执行审计缺少原准备请求"
                    )
                for audit in audits:
                    if audit.execution_job_id is not None:
                        row = await session.get(ReleaseExecutionJobRow, audit.execution_job_id)
                        if row is None:
                            raise ExecutionJobError(
                                "release_execution_integrity_violation", "执行审计缺少原任务"
                            )
                        await read_view(session, row, request)
            return ExecutionAuditPage(
                items=audits, next_cursor=audits[-1].cursor if len(rows) > limit else None
            )


def _record(
    session: AsyncSession,
    run_id: UUID,
    job_id: UUID,
    version: int,
    subject: str,
    command: ExecutionCommand,
    raw: str,
    authorization: UUID,
    *,
    now: datetime,
    identifier: UUID | None = None,
    applied: bool = False,
    error: ExecutionJobError | None = None,
) -> None:
    session.add(
        ReleaseExecutionCommandRow(
            authorization_id=authorization,
            investigation_run_id=run_id,
            candidate_job_id=job_id,
            verification_version=version,
            subject_id=subject,
            command=command.model_dump(mode="json"),
            raw_body=raw,
            command_sha256=content_hash(command),
            outcome="error" if error else "applied" if applied else "replayed",
            http_status=error.status_code if error else 201 if applied else 200,
            execution_job_id=identifier,
            error_code=error.code if error else None,
            error_message=str(error) if error else None,
            created_at=now,
        )
    )
