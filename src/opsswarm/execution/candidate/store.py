"""可信执行前检查、短时异步观测与原子不可变审计。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval.decision.authorization import check_principal
from opsswarm.execution.candidate.authorization import OPERATION, validate_admission
from opsswarm.execution.candidate.contracts import (
    POLICY_VERSION,
    PreflightAuditPage,
    PreflightCommand,
    PreflightError,
    PreflightPage,
    PreflightResult,
)
from opsswarm.execution.candidate.observer import FaultLabPreflightObserver, PreflightObserver
from opsswarm.execution.candidate.policy import build_content, validate_step
from opsswarm.execution.candidate.queries import (
    PreflightContext,
    load_context,
    matches_command,
    read_record,
    record_id,
    validated_audit,
)
from opsswarm.execution.candidate.tables import CandidatePreflightCommandRow, CandidatePreflightRow
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.candidate.store import utc_now
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.planning.candidate.budget import check_planning_budget


@dataclass(frozen=True)
class PreflightSubmission:
    result: PreflightResult | None = None
    error: PreflightError | None = None


class CandidatePreflightStore:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        observer: PreflightObserver | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._factory, self._clock = factory, clock
        self._observer = (
            observer if observer is not None else FaultLabPreflightObserver(clock=clock)
        )

    def _check(self, principal: OperatorPrincipal) -> datetime:
        now = normalized_now(self._clock)
        check_principal(principal, now)
        return now

    async def _validate_current(
        self,
        session: AsyncSession,
        run: InvestigationRunRow,
        context: PreflightContext,
        command: PreflightCommand,
        *,
        principal: OperatorPrincipal,
    ) -> datetime:
        now = self._check(principal)
        decision = context.decision.record
        assert decision is not None
        if (
            decision.id != command.decision_id
            or decision.content_sha256 != command.decision_content_sha256
        ):
            raise PreflightError(
                "candidate_preflight_decision_mismatch", "命令未绑定精确批准及完整摘要"
            )
        incident = await session.get(IncidentRow, run.incident_id)
        if (
            decision.outcome != "approved"
            or context.decision.availability != "current"
            or incident is None
            or incident.status != "mitigating"
            or incident.version != decision.incident_version
            or command.expected_incident_version != decision.incident_version
        ):
            raise PreflightError("candidate_preflight_not_current", "批准或事故阶段版本已失效")
        if now >= context.request.expires_at:
            raise PreflightError("candidate_preflight_expired", "原批准期限已到，不能继续检查")
        budget = await read_budget_snapshot(session, run)
        if any(call.status == "reserved" for call in budget.calls):
            raise PreflightError("candidate_preflight_calls_in_flight", "原运行仍有未结算调用")
        check_planning_budget(context.source, context.packet, budget, now=self._check(principal))
        return self._check(principal)

    async def create(
        self,
        run_id: UUID,
        job_id: UUID,
        command: PreflightCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> PreflightSubmission:
        require_permission(principal, "POST", OPERATION)
        self._check(principal)
        if len(raw_body) > 65536:
            raise OperatorAccessError("operator_command_too_large", "检查命令超过64 KiB", 413)
        try:
            raw = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "检查命令必须为UTF-8", 422
            ) from None
        async with self._factory() as session, session.begin():
            admission = await session.get(
                OperatorAuthorizationRow, authorization_id, with_for_update=True
            )
            admission_time = self._check(principal)
            try:
                validate_admission(
                    admission,
                    run_id=run_id,
                    job_id=job_id,
                    subject_id=principal.subject_id,
                    command=command,
                    raw_body=raw,
                    occurred_at=admission_time,
                )
                if admission is None or admission.credential_id != principal.credential_id:
                    raise ValueError("凭据不一致")
            except ValueError:
                raise OperatorAccessError(
                    "operator_permission_denied", "检查缺少同源可信准入", 403
                ) from None
            prior = await session.scalar(
                select(CandidatePreflightCommandRow).where(
                    CandidatePreflightCommandRow.authorization_id == authorization_id
                )
            )
            if prior is not None:
                audit = await validated_audit(session, prior)
                if audit.command != command or audit.subject_id != principal.subject_id:
                    raise OperatorAccessError(
                        "operator_permission_denied", "准入已绑定另一命令", 403
                    )
                if audit.outcome == "error":
                    self._check(principal)
                    return PreflightSubmission(
                        error=PreflightError(
                            audit.error_code or "candidate_preflight_conflict",
                            audit.error_message or "检查冲突",
                            audit.http_status,
                        )
                    )
            mutated = False
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
                row = await session.scalar(
                    select(CandidatePreflightRow).where(
                        CandidatePreflightRow.investigation_run_id == run_id,
                        CandidatePreflightRow.requested_by_id == principal.subject_id,
                        CandidatePreflightRow.idempotency_key == command.idempotency_key,
                    )
                )
                if row is not None:
                    if not matches_command(row, job_id, command):
                        raise PreflightError(
                            "candidate_preflight_idempotency_conflict", "幂等键已绑定另一完整命令"
                        )
                    # 已有同命令事实的损坏必须拒绝重建，不能降级成新的业务拒绝审计。
                    mutated = True
                    applied = False
                else:
                    if prior is not None:
                        raise PreflightError(
                            "candidate_preflight_integrity_violation", "原命令缺少检查事实"
                        )
                context = await load_context(session, run, job_id, clock=self._clock)
                if row is None:
                    started_at = await self._validate_current(
                        session, run, context, command, principal=principal
                    )
                    decision = context.decision.record
                    assert decision is not None
                    step = context.plan.content.plan.steps[0]
                    validate_step(step)
                    observation = await self._observer.observe(step)
                    now = await self._validate_current(
                        session, run, context, command, principal=principal
                    )
                    expires_at = min(
                        context.request.expires_at, observation.observed_at + timedelta(seconds=30)
                    )
                    if not started_at <= observation.observed_at <= now < expires_at:
                        raise PreflightError(
                            "candidate_preflight_observation_stale", "观测时间不属于本次有界检查"
                        )
                    content = build_content(decision, step, observation)
                    operation_id = uuid5(decision.id, "candidate-operation:v1")
                    row = CandidatePreflightRow(
                        id=record_id(operation_id, principal.subject_id, command.idempotency_key),
                        operation_id=operation_id,
                        investigation_run_id=run_id,
                        candidate_job_id=job_id,
                        decision_id=decision.id,
                        decision_content_sha256=decision.content_sha256,
                        incident_version=decision.incident_version,
                        policy_version=POLICY_VERSION,
                        requested_by_id=principal.subject_id,
                        authorization_id=authorization_id,
                        idempotency_key=command.idempotency_key,
                        command_sha256=content_hash(command),
                        content=content.model_dump(mode="json"),
                        content_sha256=content_hash(content),
                        created_at=now,
                        expires_at=expires_at,
                    )
                    mutated = True
                    session.add(row)
                    await session.flush()
                    applied = True
                if prior is None:
                    _record(
                        session,
                        run_id,
                        job_id,
                        principal.subject_id,
                        command,
                        raw,
                        authorization_id,
                        now=row.created_at if applied else self._check(principal),
                        preflight_id=row.id,
                        applied=applied,
                    )
                    await session.flush()
                record = await read_record(session, row, context)
                if applied:
                    now = await self._validate_current(
                        session, run, context, command, principal=principal
                    )
                    if now >= row.expires_at:
                        raise PreflightError(
                            "candidate_preflight_observation_stale", "检查提交前观测已过期"
                        )
                self._check(principal)
                return PreflightSubmission(result=PreflightResult(applied=applied, record=record))
            except OperatorAccessError:
                raise
            except (ValueError, CallBudgetExceeded, TaskFactConflict) as error:
                failure = (
                    error
                    if isinstance(error, PreflightError)
                    else PreflightError(
                        "candidate_preflight_source_invalid",
                        "检查来源或原预算无法通过核验",
                        404 if getattr(error, "status_code", None) == 404 else 409,
                    )
                )
                if mutated or prior is not None:
                    if failure is error:
                        raise
                    raise failure from error
                _record(
                    session,
                    run_id,
                    job_id,
                    principal.subject_id,
                    command,
                    raw,
                    authorization_id,
                    now=self._check(principal),
                    error=failure,
                )
                await session.flush()
                self._check(principal)
                return PreflightSubmission(error=failure)

    async def get(
        self, run_id: UUID, job_id: UUID, *, after_cursor: int = 0, limit: int = 50
    ) -> PreflightPage:
        _validate_page(after_cursor, limit)
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            context = await load_context(session, run, job_id, clock=self._clock)
            rows = list(
                await session.scalars(
                    select(CandidatePreflightRow)
                    .where(
                        CandidatePreflightRow.investigation_run_id == run_id,
                        CandidatePreflightRow.candidate_job_id == job_id,
                        CandidatePreflightRow.cursor > after_cursor,
                    )
                    .order_by(CandidatePreflightRow.cursor)
                    .limit(limit + 1)
                )
            )
            items = tuple([await read_record(session, row, context) for row in rows[:limit]])
            return PreflightPage(
                items=items, next_cursor=items[-1].cursor if len(rows) > limit else None
            )

    async def list_commands(
        self, run_id: UUID, job_id: UUID, *, after_cursor: int = 0, limit: int = 50
    ) -> PreflightAuditPage:
        _validate_page(after_cursor, limit)
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            rows = list(
                await session.scalars(
                    select(CandidatePreflightCommandRow)
                    .where(
                        CandidatePreflightCommandRow.investigation_run_id == run_id,
                        CandidatePreflightCommandRow.candidate_job_id == job_id,
                        CandidatePreflightCommandRow.cursor > after_cursor,
                    )
                    .order_by(CandidatePreflightCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            items = tuple([await validated_audit(session, row) for row in rows[:limit]])
            if any(item.preflight_id is not None for item in items):
                context = await load_context(session, run, job_id, clock=self._clock)
                for item in items:
                    if item.preflight_id is not None:
                        row = await session.get(CandidatePreflightRow, item.preflight_id)
                        assert row is not None
                        await read_record(session, row, context)
            return PreflightAuditPage(
                items=items, next_cursor=items[-1].cursor if len(rows) > limit else None
            )


def _validate_page(cursor: int, limit: int) -> None:
    if isinstance(cursor, bool) or cursor < 0 or isinstance(limit, bool) or not 1 <= limit <= 100:
        raise ValueError("检查分页参数不合法")


def _record(
    session: AsyncSession,
    run_id: UUID,
    job_id: UUID,
    subject_id: str,
    command: PreflightCommand,
    raw_body: str,
    authorization_id: UUID,
    *,
    now: datetime,
    preflight_id: UUID | None = None,
    applied: bool = False,
    error: PreflightError | None = None,
) -> None:
    session.add(
        CandidatePreflightCommandRow(
            authorization_id=authorization_id,
            investigation_run_id=run_id,
            candidate_job_id=job_id,
            subject_id=subject_id,
            command=command.model_dump(mode="json"),
            raw_body=raw_body,
            command_sha256=content_hash(command),
            outcome="error" if error else "applied" if applied else "replayed",
            http_status=error.status_code if error else 201 if applied else 200,
            preflight_id=preflight_id,
            error_code=error.code if error else None,
            error_message=str(error) if error else None,
            created_at=now,
        )
    )
