"""可信候选评估命令的精确幂等、事务审计与历史读取。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.candidate.authorization import (
    OPERATION,
    check_principal,
    validate_admission,
)
from opsswarm.investigation.candidate.budget import check_budget
from opsswarm.investigation.candidate.context import build_packet
from opsswarm.investigation.candidate.contracts import (
    POLICY_VERSION,
    CandidateAuditPage,
    CandidateError,
    CandidateJobView,
    CandidateSubmissionResult,
    EvaluateCandidateCommand,
)
from opsswarm.investigation.candidate.queries import (
    matches_command,
    read_job,
    validated_audit,
)
from opsswarm.investigation.candidate.source import load_source
from opsswarm.investigation.candidate.tables import (
    CandidateCommandRow,
    CandidateJobRow,
)
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_prepare.contracts import PreparationError
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.tasks.errors import TaskFactConflict


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class CandidateSubmission:
    result: CandidateSubmissionResult | None = None
    error: CandidateError | None = None


class CandidateStore:
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
        command: EvaluateCandidateCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> CandidateSubmission:
        require_permission(principal, "POST", OPERATION)
        self._check(principal)
        if len(raw_body) > 65536:
            raise OperatorAccessError("operator_command_too_large", "候选评估命令超过 64 KiB", 413)
        try:
            raw = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "候选评估命令必须为 UTF-8", 422
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
                    subject_id=principal.subject_id,
                    command=command,
                    raw_body=raw,
                    occurred_at=now,
                )
                if admission is None or admission.credential_id != principal.credential_id:
                    raise ValueError("凭据不一致")
            except ValueError:
                raise OperatorAccessError(
                    "operator_permission_denied", "候选评估命令缺少同源可信准入", 403
                ) from None
            prior = await session.scalar(
                select(CandidateCommandRow).where(
                    CandidateCommandRow.authorization_id == authorization_id
                )
            )
            if prior is not None:
                audit = await validated_audit(session, prior)
                if audit.investigation_run_id != run_id or audit.command_sha256 != content_hash(
                    command
                ):
                    raise OperatorAccessError(
                        "operator_permission_denied", "准入已绑定另一候选评估命令", 403
                    )
                if audit.outcome == "error":
                    self._check(principal)
                    return CandidateSubmission(
                        error=CandidateError(
                            audit.error_code or "candidate_conflict",
                            audit.error_message or "候选评估冲突",
                            audit.http_status,
                        )
                    )
                run = await lock_wait_run(session, run_id, read=True)
                job = await session.get(CandidateJobRow, audit.job_id)
                assert job is not None
                view = await read_job(session, job, run, clock=self._clock)
                self._check(principal)
                return CandidateSubmission(
                    result=CandidateSubmissionResult(applied=False, job=view)
                )
            try:
                run = await lock_wait_run(session, run_id, read=False)
                self._check(principal)
                job = await session.scalar(
                    select(CandidateJobRow).where(
                        CandidateJobRow.investigation_run_id == run_id,
                        CandidateJobRow.requested_by_id == principal.subject_id,
                        CandidateJobRow.idempotency_key == command.idempotency_key,
                    )
                )
                applied = job is None
                if job is not None:
                    if not matches_command(job, command):
                        raise CandidateError(
                            "candidate_idempotency_conflict", "幂等键已绑定另一完整候选评估命令"
                        )
                    view = await read_job(session, job, run, clock=self._clock)
                else:
                    incident = await session.scalar(
                        select(IncidentRow)
                        .where(IncidentRow.id == run.incident_id)
                        .with_for_update()
                    )
                    source, current = await load_source(
                        session, run, command.source_kind, command.source_id, clock=self._clock
                    )
                    if command.source_sha256 != source.source_sha256:
                        raise CandidateError("candidate_source_conflict", "命令不匹配完整候选来源")
                    version = source.human_route.wait_version if source.human_route else None
                    if command.expected_wait_version != version:
                        raise CandidateError("candidate_version_conflict", "候选来源等待版本不一致")
                    if not current or incident is None or incident.status != "investigating":
                        raise CandidateError(
                            "candidate_not_available", "候选来源已过期或事故不再调查中"
                        )
                    occupied = await session.scalar(
                        select(CandidateJobRow.id).where(
                            CandidateJobRow.investigation_run_id == run_id,
                            CandidateJobRow.source_kind == command.source_kind,
                            CandidateJobRow.source_id == command.source_id,
                        )
                    )
                    if occupied is not None:
                        raise CandidateError("candidate_already_exists", "该精确来源已创建候选任务")
                    packet = build_packet(source)
                    check_budget(
                        source,
                        packet,
                        await read_budget_snapshot(session, run),
                        now=self._check(principal),
                        allocate=True,
                    )
                    now = self._check(principal)
                    job = CandidateJobRow(
                        id=uuid5(run_id, f"candidate:{command.source_kind}:{command.source_id}:v1"),
                        investigation_run_id=run_id,
                        source_kind=command.source_kind,
                        source_id=command.source_id,
                        source_sha256=command.source_sha256,
                        source=source.model_dump(mode="json"),
                        source_content_sha256=content_hash(source),
                        packet=packet.model_dump(mode="json"),
                        packet_sha256=content_hash(packet),
                        wait_version=version,
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
            except (
                CandidateError,
                PreparationError,
                HumanWaitError,
                TaskFactConflict,
                CallBudgetExceeded,
                ValueError,
            ) as error:
                failure = (
                    error
                    if isinstance(error, CandidateError)
                    else CandidateError(
                        "candidate_run_not_found"
                        if isinstance(error, HumanWaitError) and error.status_code == 404
                        else "candidate_source_conflict",
                        str(error),
                        error.status_code if isinstance(error, HumanWaitError) else 409,
                    )
                )
                _record(
                    session,
                    run_id,
                    principal.subject_id,
                    command,
                    raw,
                    authorization_id,
                    now=self._check(principal),
                    error=failure,
                )
                await session.flush()
                self._check(principal)
                return CandidateSubmission(error=failure)
            assert job is not None
            _record(
                session,
                run_id,
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
            return CandidateSubmission(result=CandidateSubmissionResult(applied=applied, job=view))

    async def get(self, run_id: UUID, job_id: UUID) -> CandidateJobView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            job = await session.scalar(
                select(CandidateJobRow)
                .where(
                    CandidateJobRow.id == job_id,
                    CandidateJobRow.investigation_run_id == run_id,
                )
                .with_for_update(read=True)
            )
            if job is None:
                raise CandidateError("candidate_job_not_found", "候选评估任务不存在", 404)
            return await read_job(session, job, run, clock=self._clock)

    async def list_commands(
        self, run_id: UUID, *, after_cursor: int = 0, limit: int = 50
    ) -> CandidateAuditPage:
        if after_cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("候选评估审计分页参数不合法")
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            rows = list(
                await session.scalars(
                    select(CandidateCommandRow)
                    .where(
                        CandidateCommandRow.investigation_run_id == run_id,
                        CandidateCommandRow.cursor > after_cursor,
                    )
                    .order_by(CandidateCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            records = tuple([await validated_audit(session, row) for row in rows[:limit]])
            for job_id in {item.job_id for item in records if item.job_id is not None}:
                job = await session.get(CandidateJobRow, job_id)
                assert job is not None
                await read_job(session, job, run, clock=self._clock)
            return CandidateAuditPage(
                items=records, next_cursor=records[-1].cursor if len(rows) > limit else None
            )


def _record(
    session: AsyncSession,
    run_id: UUID,
    subject_id: str,
    command: EvaluateCandidateCommand,
    raw_body: str,
    authorization_id: UUID,
    *,
    now: datetime,
    job_id: UUID | None = None,
    applied: bool = False,
    error: CandidateError | None = None,
) -> None:
    session.add(
        CandidateCommandRow(
            authorization_id=authorization_id,
            investigation_run_id=run_id,
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
