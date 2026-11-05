"""可信候选决定、原子事故迁移与完整命令幂等审计。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval.reinvestigation_decision.authorization import (
    OPERATION,
    check_principal,
    validate_admission,
)
from opsswarm.approval.reinvestigation_decision.contracts import (
    POLICY_VERSION,
    ReflowDecisionAuditPage,
    ReflowDecisionCommand,
    ReflowDecisionError,
    ReflowDecisionSubmissionResult,
    ReflowDecisionView,
)
from opsswarm.approval.reinvestigation_decision.policy import (
    build_content,
    target,
    validate_timing,
    validate_transition,
)
from opsswarm.approval.reinvestigation_decision.queries import (
    load_context,
    matches_command,
    read_decision,
    validated_audit,
)
from opsswarm.approval.reinvestigation_decision.tables import (
    ReflowApprovalDecisionCommandRow,
    ReflowApprovalDecisionRow,
)
from opsswarm.identity.models import (
    OperatorAccessError,
    OperatorPrincipal,
    require_permission,
)
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents import ActorType, IncidentActor
from opsswarm.incidents.persistence.repository import transition_incident_in_session
from opsswarm.incidents.persistence.tables import IncidentEventRow, IncidentRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.candidate.store import utc_now
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.reinvestigation_candidate.job_store import _lock_basis
from opsswarm.investigation.reinvestigation_candidate.tables import ReflowCandidateJobRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.planning.reinvestigation_candidate.budget import check_planning_budget


@dataclass(frozen=True)
class ReflowDecisionSubmission:
    result: ReflowDecisionSubmissionResult | None = None
    error: ReflowDecisionError | None = None


class ReflowApprovalDecisionStore:
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
        job_id: UUID,
        command: ReflowDecisionCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> ReflowDecisionSubmission:
        require_permission(principal, "POST", OPERATION)
        self._check(principal)
        if len(raw_body) > 65536:
            raise OperatorAccessError("operator_command_too_large", "审批决定命令超过64 KiB", 413)
        try:
            raw = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "审批决定命令必须为UTF-8", 422
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
                    "operator_permission_denied", "审批决定缺少同源可信准入", 403
                ) from None
            prior = await session.scalar(
                select(ReflowApprovalDecisionCommandRow).where(
                    ReflowApprovalDecisionCommandRow.authorization_id == authorization_id
                )
            )
            if prior is not None:
                audit = await validated_audit(session, prior)
                if (
                    audit.investigation_run_id != run_id
                    or audit.candidate_job_id != job_id
                    or audit.command != command
                ):
                    raise OperatorAccessError(
                        "operator_permission_denied", "准入已经绑定另一审批决定命令", 403
                    )
                if audit.outcome == "error":
                    self._check(principal)
                    return ReflowDecisionSubmission(
                        error=ReflowDecisionError(
                            audit.error_code or "reflow_decision_conflict",
                            audit.error_message or "审批决定冲突",
                            audit.http_status,
                        )
                    )
                run = await lock_wait_run(session, run_id, read=True)
                view = await read_decision(session, run, job_id, clock=self._clock)
                self._check(principal)
                return ReflowDecisionSubmission(
                    result=ReflowDecisionSubmissionResult(applied=False, decision=view)
                )
            mutated = False
            try:
                run = await lock_wait_run(session, run_id, read=False)
                await session.scalar(
                    select(ReflowCandidateJobRow)
                    .where(
                        ReflowCandidateJobRow.id == job_id,
                        ReflowCandidateJobRow.investigation_run_id == run_id,
                    )
                    .with_for_update()
                )
                await _lock_basis(session, run)
                incident = await session.scalar(
                    select(IncidentRow).where(IncidentRow.id == run.incident_id).with_for_update()
                )
                self._check(principal)
                row = await session.scalar(
                    select(ReflowApprovalDecisionRow).where(
                        ReflowApprovalDecisionRow.investigation_run_id == run_id,
                        ReflowApprovalDecisionRow.decided_by_id == principal.subject_id,
                        ReflowApprovalDecisionRow.idempotency_key == command.idempotency_key,
                    )
                )
                applied = row is None
                if row is not None:
                    if not matches_command(row, job_id, command):
                        raise ReflowDecisionError(
                            "reflow_decision_idempotency_conflict",
                            "幂等键已绑定另一完整审批决定命令",
                        )
                    view = await read_decision(session, run, job_id, clock=self._clock)
                else:
                    context = await load_context(session, run, job_id, clock=self._clock)
                    request = context.request.record
                    assert request is not None
                    now = self._check(principal)
                    validate_transition(
                        command,
                        request,
                        availability=context.request.availability,
                        incident_status=incident.status if incident else None,
                        incident_version=incident.version if incident else None,
                        now=now,
                    )
                    if (
                        await session.scalar(
                            select(ReflowApprovalDecisionRow.id).where(
                                ReflowApprovalDecisionRow.request_id == request.id
                            )
                        )
                        is not None
                    ):
                        raise ReflowDecisionError(
                            "reflow_decision_already_exists", "该请求已有另一不可变决定"
                        )
                    if command.action == "approve":
                        budget = await read_budget_snapshot(session, run)
                        if any(call.status == "reserved" for call in budget.calls):
                            raise ReflowDecisionError(
                                "reflow_decision_calls_in_flight", "原运行仍有未结算调用"
                            )
                        check_planning_budget(context.source, context.packet, budget, now=now)
                    content = build_content(
                        request,
                        context.plan,
                        action=command.action,
                    )
                    outcome, action, _ = target(command.action)
                    now = self._check(principal)
                    validate_timing(command.action, expires_at=request.expires_at, now=now)
                    basis_event_cursor = await session.scalar(
                        select(func.max(IncidentEventRow.id)).where(
                            IncidentEventRow.incident_id == run.incident_id
                        )
                    )
                    if basis_event_cursor is None:
                        raise ReflowDecisionError(
                            "reflow_decision_source_invalid", "决定缺少迁移前事件依据"
                        )
                    # 此后任何失败都应回滚决定与状态，不能提交半个审批事务。
                    mutated = True
                    updated = await transition_incident_in_session(
                        session,
                        run.incident_id,
                        action=action,
                        expected_version=command.expected_incident_version,
                        occurred_at=now,
                        actor=IncidentActor(
                            actor_type=ActorType.HUMAN, actor_id=principal.subject_id
                        ),
                        reason=command.reason,
                    )
                    event = await session.scalar(
                        select(IncidentEventRow).where(
                            IncidentEventRow.incident_id == run.incident_id,
                            IncidentEventRow.incident_version == updated.incident.version,
                            IncidentEventRow.event_type == "incident.status_changed",
                        )
                    )
                    assert event is not None
                    row = ReflowApprovalDecisionRow(
                        id=uuid5(request.id, "reinvestigation-approval-decision:v1"),
                        investigation_run_id=run_id,
                        candidate_job_id=job_id,
                        request_id=request.id,
                        plan_content_sha256=request.plan_content_sha256,
                        basis_event_cursor=basis_event_cursor,
                        credential_expires_at=principal.expires_at,
                        previous_incident_version=request.incident_version,
                        incident_version=updated.incident.version,
                        event_cursor=event.id,
                        outcome=outcome,
                        policy_version=POLICY_VERSION,
                        decided_by_id=principal.subject_id,
                        authorization_id=authorization_id,
                        idempotency_key=command.idempotency_key,
                        command_sha256=content_hash(command),
                        content=content.model_dump(mode="json"),
                        content_sha256=content_hash(content),
                        created_at=now,
                    )
                    session.add(row)
                    await session.flush()
            except OperatorAccessError:
                raise
            except (ValueError, CallBudgetExceeded, TaskFactConflict) as error:
                if mutated:
                    raise
                failure = (
                    error
                    if isinstance(error, ReflowDecisionError)
                    else ReflowDecisionError(
                        "reflow_decision_run_not_found"
                        if isinstance(error, HumanWaitError) and error.status_code == 404
                        else error.code
                        if isinstance(error, CallBudgetExceeded)
                        else "reflow_decision_source_invalid",
                        "候选审批决定来源或预算无法通过核验",
                        error.status_code if isinstance(error, HumanWaitError) else 409,
                    )
                )
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
                    credential_expires_at=principal.expires_at,
                )
                await session.flush()
                self._check(principal)
                return ReflowDecisionSubmission(error=failure)
            assert row is not None
            _record(
                session,
                run_id,
                job_id,
                principal.subject_id,
                command,
                raw,
                authorization_id,
                now=row.created_at if applied else self._check(principal),
                decision_id=row.id,
                applied=applied,
                credential_expires_at=principal.expires_at,
            )
            await session.flush()
            if applied:
                assert request is not None
                validate_timing(
                    command.action, expires_at=request.expires_at, now=self._check(principal)
                )
                if command.action == "approve":
                    check_planning_budget(
                        context.source,
                        context.packet,
                        await read_budget_snapshot(session, run),
                        now=self._check(principal),
                    )
                view = await read_decision(session, run, job_id, clock=self._clock)
                if command.action == "approve":
                    check_planning_budget(
                        context.source,
                        context.packet,
                        await read_budget_snapshot(session, run),
                        now=self._check(principal),
                    )
                validate_timing(
                    command.action, expires_at=request.expires_at, now=self._check(principal)
                )
            self._check(principal)
            return ReflowDecisionSubmission(
                result=ReflowDecisionSubmissionResult(applied=applied, decision=view)
            )

    async def get(self, run_id: UUID, job_id: UUID) -> ReflowDecisionView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            return await read_decision(session, run, job_id, clock=self._clock)

    async def list_commands(
        self, run_id: UUID, job_id: UUID, *, after_cursor: int = 0, limit: int = 50
    ) -> ReflowDecisionAuditPage:
        if after_cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("审批决定审计分页参数不合法")
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            rows = list(
                await session.scalars(
                    select(ReflowApprovalDecisionCommandRow)
                    .where(
                        ReflowApprovalDecisionCommandRow.investigation_run_id == run_id,
                        ReflowApprovalDecisionCommandRow.candidate_job_id == job_id,
                        ReflowApprovalDecisionCommandRow.cursor > after_cursor,
                    )
                    .order_by(ReflowApprovalDecisionCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            records = tuple([await validated_audit(session, row) for row in rows[:limit]])
            if any(record.decision_id is not None for record in records):
                await read_decision(session, run, job_id, clock=self._clock)
            return ReflowDecisionAuditPage(
                items=records, next_cursor=records[-1].cursor if len(rows) > limit else None
            )


def _record(
    session: AsyncSession,
    run_id: UUID,
    job_id: UUID,
    subject_id: str,
    command: ReflowDecisionCommand,
    raw_body: str,
    authorization_id: UUID,
    *,
    now: datetime,
    credential_expires_at: datetime,
    decision_id: UUID | None = None,
    applied: bool = False,
    error: ReflowDecisionError | None = None,
) -> None:
    session.add(
        ReflowApprovalDecisionCommandRow(
            authorization_id=authorization_id,
            investigation_run_id=run_id,
            candidate_job_id=job_id,
            subject_id=subject_id,
            command=command.model_dump(mode="json"),
            raw_body=raw_body,
            command_sha256=content_hash(command),
            outcome="error" if error else "applied" if applied else "replayed",
            http_status=error.status_code if error else 201 if applied else 200,
            decision_id=decision_id,
            error_code=error.code if error else None,
            error_message=str(error) if error else None,
            credential_expires_at=credential_expires_at,
            created_at=now,
        )
    )
