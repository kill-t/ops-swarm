"""可信核验版本的事务准入、连续追加与只读适用性。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval.decision.authorization import check_principal
from opsswarm.execution.candidate.queries import PreflightContext
from opsswarm.execution.conditions.authorization import OPERATION, validate_admission
from opsswarm.execution.conditions.contracts import (
    POLICY_VERSION,
    ConditionAuditPage,
    ConditionCommand,
    ConditionError,
    ConditionRecord,
    ConditionResult,
    ConditionState,
    ConditionVersionView,
)
from opsswarm.execution.conditions.policy import (
    build_catalog,
    build_content,
    expires_at,
    validate_version,
    verification_availability,
)
from opsswarm.execution.conditions.queries import (
    matches_command,
    read_chain,
    validated_audit,
    verification_id,
)
from opsswarm.execution.conditions.sources import ConditionSources, load_context
from opsswarm.execution.conditions.tables import CandidateConditionCommandRow, CandidateConditionRow
from opsswarm.identity.models import (
    OperatorAccessError,
    OperatorPrincipal,
    OperatorRole,
    require_permission,
)
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
from opsswarm.planning.candidate.contracts import PlanError


@dataclass(frozen=True)
class ConditionSubmission:
    result: ConditionResult | None = None
    error: ConditionError | None = None


class CandidateConditionStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock

    def _check(self, principal: OperatorPrincipal) -> datetime:
        now = normalized_now(self._clock)
        check_principal(principal, now)
        return now

    async def _budget_current(
        self,
        session: AsyncSession,
        run: InvestigationRunRow,
        context: PreflightContext,
        *,
        now: datetime,
    ) -> bool:
        try:
            budget = await read_budget_snapshot(session, run)
        except (ValueError, TaskFactConflict) as error:
            raise ConditionError(
                "candidate_condition_source_invalid", "原预算账本无法完整重建"
            ) from error
        if any(call.status == "reserved" for call in budget.calls):
            return False
        try:
            check_planning_budget(context.source, context.packet, budget, now=now)
        except (PlanError, CallBudgetExceeded):
            return False
        return True

    async def _require_current(
        self,
        session: AsyncSession,
        run: InvestigationRunRow,
        context: PreflightContext,
        command: ConditionCommand,
        *,
        principal: OperatorPrincipal,
    ) -> datetime:
        now = self._check(principal)
        decision = context.decision.record
        assert decision is not None
        incident = await session.get(IncidentRow, run.incident_id)
        if (
            decision.id != command.decision_id
            or decision.content_sha256 != command.decision_content_sha256
            or decision.outcome != "approved"
            or context.decision.availability != "current"
            or incident is None
            or incident.status != "mitigating"
            or incident.version != decision.incident_version
            or command.expected_incident_version != decision.incident_version
            or now >= context.request.expires_at
        ):
            raise ConditionError(
                "candidate_condition_not_current", "原批准、输入、事故版本或期限已失效"
            )
        if not await self._budget_current(session, run, context, now=self._check(principal)):
            raise ConditionError(
                "candidate_condition_budget_not_current", "原预算超额、到期或仍有在途调用"
            )
        return self._check(principal)

    async def create(
        self,
        run_id: UUID,
        job_id: UUID,
        command: ConditionCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> ConditionSubmission:
        require_permission(principal, "POST", OPERATION)
        self._check(principal)
        if principal.role is not OperatorRole.ENGINEER:
            raise OperatorAccessError(
                "operator_permission_denied", "仅可信工程师可以提交条件核验", 403
            )
        if len(raw_body) > 65536:
            raise OperatorAccessError("operator_command_too_large", "核验命令超过64KiB", 413)
        try:
            raw = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "核验命令必须为UTF-8", 422
            ) from None
        async with self._factory() as session, session.begin():
            admission = await session.get(
                OperatorAuthorizationRow, authorization_id, with_for_update=True
            )
            at = self._check(principal)
            try:
                validate_admission(
                    admission,
                    run_id=run_id,
                    job_id=job_id,
                    subject_id=principal.subject_id,
                    command=command,
                    raw_body=raw,
                    occurred_at=at,
                )
                if admission is None or admission.credential_id != principal.credential_id:
                    raise ValueError("凭据不一致")
            except ValueError:
                raise OperatorAccessError(
                    "operator_permission_denied", "核验缺少同源工程师准入", 403
                ) from None
            prior = await session.scalar(
                select(CandidateConditionCommandRow).where(
                    CandidateConditionCommandRow.authorization_id == authorization_id
                )
            )
            if prior is not None:
                audit = await validated_audit(session, prior)
                if audit.command != command or audit.subject_id != principal.subject_id:
                    raise OperatorAccessError(
                        "operator_permission_denied", "准入已绑定另一核验命令", 403
                    )
                if audit.outcome == "error":
                    self._check(principal)
                    return ConditionSubmission(
                        error=ConditionError(
                            audit.error_code or "candidate_condition_conflict",
                            audit.error_message or "核验冲突",
                            audit.http_status,
                        )
                    )
            rollback_only = False
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
                    select(CandidateConditionRow).where(
                        CandidateConditionRow.investigation_run_id == run_id,
                        CandidateConditionRow.verified_by_id == principal.subject_id,
                        CandidateConditionRow.idempotency_key == command.idempotency_key,
                    )
                )
                if row is not None:
                    if not matches_command(row, job_id, command):
                        raise ConditionError(
                            "candidate_condition_idempotency_conflict", "幂等键已绑定另一完整核验"
                        )
                    # 已有事实损坏时不能保存一个较弱的error来代替历史。
                    rollback_only = True
                elif prior is not None:
                    raise ConditionError(
                        "candidate_condition_integrity_violation", "原命令缺少核验版本"
                    )
                context = await load_context(session, run, job_id, clock=self._clock)
                sources = ConditionSources(session, context)
                chain = await read_chain(session, sources)
                applied = row is None
                if applied:
                    now = await self._require_current(
                        session, run, context, command, principal=principal
                    )
                    latest = chain[-1] if chain else None
                    validate_version(command, latest)
                    decision = context.decision.record
                    assert decision is not None
                    content = build_content(decision, context.plan, command)
                    deadlines = await sources.validate(command.items, at=now)
                    deadline = expires_at(now, context.request.expires_at, command.items, deadlines)
                    if self._check(principal) >= deadline:
                        raise ConditionError("candidate_condition_source_expired", "核验来源已到期")
                    version = command.expected_condition_version + 1
                    row = CandidateConditionRow(
                        id=verification_id(decision.id, version),
                        investigation_run_id=run_id,
                        candidate_job_id=job_id,
                        decision_id=decision.id,
                        decision_content_sha256=decision.content_sha256,
                        version=version,
                        previous_id=latest.id if latest else None,
                        previous_content_sha256=latest.content_sha256 if latest else None,
                        incident_version=decision.incident_version,
                        policy_version=POLICY_VERSION,
                        verified_by_id=principal.subject_id,
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
                assert row is not None
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
                        verification=row.id,
                        applied=applied,
                    )
                    await session.flush()
                chain = await read_chain(session, sources)
                record = next((item for item in chain if item.id == row.id), None)
                if record is None:
                    raise ConditionError(
                        "candidate_condition_integrity_violation", "核验不属于完整版本前缀"
                    )
                view = await self._view(session, run, context, chain, record)
                if applied:
                    now = await self._require_current(
                        session, run, context, command, principal=principal
                    )
                    if now >= row.expires_at:
                        raise ConditionError(
                            "candidate_condition_source_expired", "核验提交前来源已到期"
                        )
                self._check(principal)
                return ConditionSubmission(
                    result=ConditionResult(applied=applied, verification=view)
                )
            except OperatorAccessError:
                raise
            except (ValueError, CallBudgetExceeded, TaskFactConflict) as error:
                failure = (
                    error
                    if isinstance(error, ConditionError)
                    else ConditionError(
                        "candidate_condition_source_invalid",
                        "核验来源、版本或原预算无法完整重建",
                        404 if getattr(error, "status_code", None) == 404 else 409,
                    )
                )
                if rollback_only or prior is not None:
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
                return ConditionSubmission(error=failure)

    async def _view(
        self,
        session: AsyncSession,
        run: InvestigationRunRow,
        context: PreflightContext,
        chain: tuple[ConditionRecord, ...],
        record: ConditionRecord,
    ) -> ConditionVersionView:
        budget_current = await self._budget_current(
            session, run, context, now=normalized_now(self._clock)
        )
        now = normalized_now(self._clock)
        return ConditionVersionView(
            record=record,
            availability=verification_availability(
                record,
                chain[-1].version,
                context.decision.availability,
                now,
                budget_current=budget_current,
            ),
        )

    async def get_state(self, run_id: UUID, job_id: UUID) -> ConditionState:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            context = await load_context(session, run, job_id, clock=self._clock)
            chain = await read_chain(session, ConditionSources(session, context))
            if chain:
                availability = (
                    await self._view(session, run, context, chain, chain[-1])
                ).availability
            else:
                now = normalized_now(self._clock)
                availability = (
                    "expired"
                    if now >= context.request.expires_at
                    else (
                        "pending"
                        if context.decision.availability == "current"
                        and await self._budget_current(session, run, context, now=now)
                        else "stale"
                    )
                )
            return ConditionState(
                catalog=build_catalog(context.plan),
                condition_version=len(chain),
                latest=chain[-1] if chain else None,
                availability=availability,
            )

    async def get_version(self, run_id: UUID, job_id: UUID, version: int) -> ConditionVersionView:
        if isinstance(version, bool) or not 1 <= version <= 8:
            raise ConditionError("candidate_condition_version_not_found", "核验版本不存在", 404)
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            context = await load_context(session, run, job_id, clock=self._clock)
            chain = await read_chain(session, ConditionSources(session, context))
            if version > len(chain):
                raise ConditionError("candidate_condition_version_not_found", "核验版本不存在", 404)
            return await self._view(session, run, context, chain, chain[version - 1])

    async def list_commands(
        self, run_id: UUID, job_id: UUID, *, after_cursor: int = 0, limit: int = 50
    ) -> ConditionAuditPage:
        if (
            isinstance(after_cursor, bool)
            or after_cursor < 0
            or isinstance(limit, bool)
            or not 1 <= limit <= 100
        ):
            raise ValueError("核验审计分页参数不合法")
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            rows = list(
                await session.scalars(
                    select(CandidateConditionCommandRow)
                    .where(
                        CandidateConditionCommandRow.investigation_run_id == run_id,
                        CandidateConditionCommandRow.candidate_job_id == job_id,
                        CandidateConditionCommandRow.cursor > after_cursor,
                    )
                    .order_by(CandidateConditionCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            items = tuple([await validated_audit(session, row) for row in rows[:limit]])
            if any(item.verification_id is not None for item in items):
                context = await load_context(session, run, job_id, clock=self._clock)
                chain = await read_chain(session, ConditionSources(session, context))
                identifiers = {record.id for record in chain}
                if any(
                    item.verification_id is not None and item.verification_id not in identifiers
                    for item in items
                ):
                    raise ConditionError(
                        "candidate_condition_integrity_violation", "审计引用缺失核验版本"
                    )
            return ConditionAuditPage(
                items=items, next_cursor=items[-1].cursor if len(rows) > limit else None
            )


def _record(
    session: AsyncSession,
    run_id: UUID,
    job_id: UUID,
    subject: str,
    command: ConditionCommand,
    raw: str,
    authorization_id: UUID,
    *,
    now: datetime,
    verification: UUID | None = None,
    applied: bool = False,
    error: ConditionError | None = None,
) -> None:
    session.add(
        CandidateConditionCommandRow(
            authorization_id=authorization_id,
            investigation_run_id=run_id,
            candidate_job_id=job_id,
            subject_id=subject,
            command=command.model_dump(mode="json"),
            raw_body=raw,
            command_sha256=content_hash(command),
            outcome="error" if error else "applied" if applied else "replayed",
            http_status=error.status_code if error else 201 if applied else 200,
            verification_id=verification,
            error_code=error.code if error else None,
            error_message=str(error) if error else None,
            created_at=now,
        )
    )
