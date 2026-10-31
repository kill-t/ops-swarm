"""可信候选审批请求、事故原子迁移与精确幂等审计。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval.reinvestigation_candidate.authorization import (
    OPERATION,
    check_principal,
    validate_admission,
)
from opsswarm.approval.reinvestigation_candidate.contracts import (
    POLICY_VERSION,
    ReflowRequestAuditPage,
    ReflowRequestCommand,
    ReflowRequestError,
    ReflowRequestSubmissionResult,
    ReflowRequestView,
)
from opsswarm.approval.reinvestigation_candidate.policy import request_expiry
from opsswarm.approval.reinvestigation_candidate.queries import (
    matches_command,
    plan_source,
    read_request,
    validated_audit,
)
from opsswarm.approval.reinvestigation_candidate.tables import (
    ReflowApprovalCommandRow,
    ReflowApprovalRequestRow,
)
from opsswarm.identity.models import (
    OperatorAccessError,
    OperatorPermission,
    OperatorPrincipal,
    require_permission,
)
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents import ActorType, IncidentAction, IncidentActor
from opsswarm.incidents.persistence.repository import transition_incident_in_session
from opsswarm.incidents.persistence.tables import IncidentEventRow, IncidentRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.candidate.store import utc_now
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.reinvestigation_candidate.contracts import ReflowCandidatePacket
from opsswarm.investigation.reinvestigation_candidate.job_store import _lock_basis
from opsswarm.investigation.reinvestigation_candidate.tables import ReflowCandidateJobRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.planning.reinvestigation_candidate.budget import check_planning_budget


@dataclass(frozen=True)
class ReflowRequestSubmission:
    result: ReflowRequestSubmissionResult | None = None
    error: ReflowRequestError | None = None


class ReflowApprovalStore:
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
        command: ReflowRequestCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> ReflowRequestSubmission:
        require_permission(principal, "POST", OPERATION)
        if OperatorPermission.MANAGE_INCIDENT not in principal.permissions:
            raise OperatorAccessError("operator_permission_denied", "审批请求需要事故管理权限", 403)
        self._check(principal)
        if len(raw_body) > 65536:
            raise OperatorAccessError("operator_command_too_large", "审批请求命令超过64 KiB", 413)
        try:
            raw = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "审批请求命令必须为UTF-8", 422
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
                    "operator_permission_denied", "审批请求缺少同源可信准入", 403
                ) from None
            prior = await session.scalar(
                select(ReflowApprovalCommandRow).where(
                    ReflowApprovalCommandRow.authorization_id == authorization_id
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
                        "operator_permission_denied", "准入已经绑定另一审批请求命令", 403
                    )
                if audit.outcome == "error":
                    self._check(principal)
                    return ReflowRequestSubmission(
                        error=ReflowRequestError(
                            audit.error_code or "reflow_approval_conflict",
                            audit.error_message or "审批请求冲突",
                            audit.http_status,
                        )
                    )
                run = await lock_wait_run(session, run_id, read=True)
                view = await read_request(session, run, job_id, clock=self._clock)
                self._check(principal)
                return ReflowRequestSubmission(
                    result=ReflowRequestSubmissionResult(applied=False, request=view)
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
                    select(ReflowApprovalRequestRow).where(
                        ReflowApprovalRequestRow.investigation_run_id == run_id,
                        ReflowApprovalRequestRow.requested_by_id == principal.subject_id,
                        ReflowApprovalRequestRow.idempotency_key == command.idempotency_key,
                    )
                )
                applied = row is None
                if row is not None:
                    if not matches_command(row, job_id, command):
                        raise ReflowRequestError(
                            "reflow_approval_request_idempotency_conflict",
                            "幂等键已绑定另一完整审批请求命令",
                        )
                    view = await read_request(session, run, job_id, clock=self._clock)
                else:
                    plan, source, availability = await plan_source(
                        session, run, job_id, clock=self._clock
                    )
                    if command.plan_content_sha256 != plan.content_sha256:
                        raise ReflowRequestError(
                            "reflow_approval_plan_mismatch", "命令不匹配完整草案内容"
                        )
                    if (
                        availability != "current"
                        or incident is None
                        or incident.status != "investigating"
                    ):
                        raise ReflowRequestError(
                            "reflow_approval_source_stale", "草案输入或事故状态已变化"
                        )
                    if incident.version != command.expected_incident_version:
                        raise ReflowRequestError(
                            "reflow_approval_incident_version_conflict", "事故版本已经变化"
                        )
                    if (
                        await session.scalar(
                            select(ReflowApprovalRequestRow.id).where(
                                ReflowApprovalRequestRow.plan_id == plan.id
                            )
                        )
                        is not None
                    ):
                        raise ReflowRequestError(
                            "reflow_approval_already_exists", "草案已绑定另一可信审批请求"
                        )
                    job = await session.get(ReflowCandidateJobRow, job_id)
                    assert job is not None
                    packet = ReflowCandidatePacket.model_validate(job.packet)
                    budget = await read_budget_snapshot(session, run)
                    if any(call.status == "reserved" for call in budget.calls):
                        raise ReflowRequestError(
                            "reflow_approval_calls_in_flight", "原运行仍有未结算调用"
                        )
                    now = self._check(principal)
                    check_planning_budget(source, packet, budget, now=now)
                    expires_at = request_expiry(
                        started_at=source.started_at,
                        max_duration_seconds=source.call_budget.max_duration_seconds,
                        now=now,
                    )
                    basis_event_cursor = await session.scalar(
                        select(func.max(IncidentEventRow.id)).where(
                            IncidentEventRow.incident_id == run.incident_id
                        )
                    )
                    if basis_event_cursor is None:
                        raise ReflowRequestError(
                            "reflow_approval_source_invalid", "审批请求缺少迁移前事件依据"
                        )
                    # 所有业务拒绝均在迁移前确定；此后的异常必须回滚整个事务。
                    mutated = True
                    updated = await transition_incident_in_session(
                        session,
                        run.incident_id,
                        action=IncidentAction.REQUEST_APPROVAL,
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
                    row = ReflowApprovalRequestRow(
                        id=uuid5(plan.id, "reinvestigation-approval-request:v1"),
                        investigation_run_id=run_id,
                        candidate_job_id=job_id,
                        plan_id=plan.id,
                        plan_content_sha256=plan.content_sha256,
                        basis_event_cursor=basis_event_cursor,
                        credential_expires_at=principal.expires_at,
                        previous_incident_version=command.expected_incident_version,
                        incident_version=updated.incident.version,
                        event_cursor=event.id,
                        status="pending",
                        policy_version=POLICY_VERSION,
                        requested_by_id=principal.subject_id,
                        authorization_id=authorization_id,
                        idempotency_key=command.idempotency_key,
                        command_sha256=content_hash(command),
                        created_at=now,
                        expires_at=expires_at,
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
                    if isinstance(error, ReflowRequestError)
                    else ReflowRequestError(
                        "reflow_approval_run_not_found"
                        if isinstance(error, HumanWaitError) and error.status_code == 404
                        else error.code
                        if isinstance(error, CallBudgetExceeded)
                        else "reflow_approval_source_invalid",
                        "候选审批来源或预算无法通过核验",
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
                return ReflowRequestSubmission(error=failure)
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
                request_id=row.id,
                applied=applied,
                credential_expires_at=principal.expires_at,
            )
            await session.flush()
            if applied:
                # 写入阶段耗时不能把已过期的准入变成可提交事实。
                check_planning_budget(
                    source,
                    packet,
                    await read_budget_snapshot(session, run),
                    now=self._check(principal),
                )
                view = await read_request(session, run, job_id, clock=self._clock)
                check_planning_budget(
                    source,
                    packet,
                    await read_budget_snapshot(session, run),
                    now=self._check(principal),
                )
            self._check(principal)
            return ReflowRequestSubmission(
                result=ReflowRequestSubmissionResult(applied=applied, request=view)
            )

    async def get(self, run_id: UUID, job_id: UUID) -> ReflowRequestView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            return await read_request(session, run, job_id, clock=self._clock)

    async def list_commands(
        self, run_id: UUID, job_id: UUID, *, after_cursor: int = 0, limit: int = 50
    ) -> ReflowRequestAuditPage:
        if after_cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("审批请求审计分页参数不合法")
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            rows = list(
                await session.scalars(
                    select(ReflowApprovalCommandRow)
                    .where(
                        ReflowApprovalCommandRow.investigation_run_id == run_id,
                        ReflowApprovalCommandRow.candidate_job_id == job_id,
                        ReflowApprovalCommandRow.cursor > after_cursor,
                    )
                    .order_by(ReflowApprovalCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            records = tuple([await validated_audit(session, row) for row in rows[:limit]])
            if any(record.request_id is not None for record in records):
                await read_request(session, run, job_id, clock=self._clock)
            return ReflowRequestAuditPage(
                items=records, next_cursor=records[-1].cursor if len(rows) > limit else None
            )


def _record(
    session: AsyncSession,
    run_id: UUID,
    job_id: UUID,
    subject_id: str,
    command: ReflowRequestCommand,
    raw_body: str,
    authorization_id: UUID,
    *,
    now: datetime,
    credential_expires_at: datetime,
    request_id: UUID | None = None,
    applied: bool = False,
    error: ReflowRequestError | None = None,
) -> None:
    session.add(
        ReflowApprovalCommandRow(
            authorization_id=authorization_id,
            investigation_run_id=run_id,
            candidate_job_id=job_id,
            subject_id=subject_id,
            command=command.model_dump(mode="json"),
            raw_body=raw_body,
            command_sha256=content_hash(command),
            outcome="error" if error else "applied" if applied else "replayed",
            http_status=error.status_code if error else 201 if applied else 200,
            request_id=request_id,
            error_code=error.code if error else None,
            error_message=str(error) if error else None,
            credential_expires_at=credential_expires_at,
            created_at=now,
        )
    )
