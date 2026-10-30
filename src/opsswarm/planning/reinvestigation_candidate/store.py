"""可信候选草案准入、精确幂等与原子业务审计。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.identity.models import (
    OperatorAccessError,
    OperatorPermission,
    OperatorPrincipal,
    require_permission,
)
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.candidate.store import utc_now
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.reinvestigation_candidate.job_store import _lock_basis
from opsswarm.investigation.reinvestigation_candidate.tables import ReflowCandidateJobRow
from opsswarm.investigation.reinvestigation_routing.source import current_basis
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.planning.reinvestigation_candidate.authorization import (
    OPERATION,
    check_principal,
    validate_admission,
)
from opsswarm.planning.reinvestigation_candidate.budget import check_planning_budget
from opsswarm.planning.reinvestigation_candidate.contracts import (
    POLICY_VERSION,
    ReflowPlanAuditPage,
    ReflowPlanCommand,
    ReflowPlanError,
    ReflowPlanSubmissionResult,
    ReflowPlanView,
)
from opsswarm.planning.reinvestigation_candidate.policy import build_plan
from opsswarm.planning.reinvestigation_candidate.queries import (
    candidate_packet,
    matches_command,
    read_plan,
    validated_audit,
)
from opsswarm.planning.reinvestigation_candidate.tables import ReflowPlanCommandRow, ReflowPlanRow


@dataclass(frozen=True)
class ReflowPlanSubmission:
    result: ReflowPlanSubmissionResult | None = None
    error: ReflowPlanError | None = None


class ReflowPlanStore:
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
        command: ReflowPlanCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> ReflowPlanSubmission:
        require_permission(principal, "POST", OPERATION)
        if OperatorPermission.MANAGE_INCIDENT not in principal.permissions:
            raise OperatorAccessError("operator_permission_denied", "规划需要事故管理权限", 403)
        self._check(principal)
        if len(raw_body) > 65536:
            raise OperatorAccessError("operator_command_too_large", "规划命令超过64 KiB", 413)
        try:
            raw = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "规划命令必须为UTF-8", 422
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
                    "operator_permission_denied", "规划缺少同源可信准入", 403
                ) from None
            prior = await session.scalar(
                select(ReflowPlanCommandRow).where(
                    ReflowPlanCommandRow.authorization_id == authorization_id
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
                        "operator_permission_denied", "准入已经绑定另一规划命令", 403
                    )
                if audit.outcome == "error":
                    self._check(principal)
                    return ReflowPlanSubmission(
                        error=ReflowPlanError(
                            audit.error_code or "reflow_plan_conflict",
                            audit.error_message or "规划冲突",
                            audit.http_status,
                        )
                    )
                run = await lock_wait_run(session, run_id, read=True)
                view = await read_plan(session, run, job_id, clock=self._clock)
                self._check(principal)
                return ReflowPlanSubmission(
                    result=ReflowPlanSubmissionResult(applied=False, plan=view)
                )
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
                    select(ReflowPlanRow).where(
                        ReflowPlanRow.investigation_run_id == run_id,
                        ReflowPlanRow.requested_by_id == principal.subject_id,
                        ReflowPlanRow.idempotency_key == command.idempotency_key,
                    )
                )
                applied = row is None
                if row is not None:
                    if not matches_command(row, job_id, command):
                        raise ReflowPlanError(
                            "reflow_plan_idempotency_conflict", "幂等键已绑定另一完整规划命令"
                        )
                    view = await read_plan(session, run, job_id, clock=self._clock)
                else:
                    candidate, source, packet = await candidate_packet(
                        session, run, job_id, clock=self._clock
                    )
                    if candidate.status != "succeeded" or candidate.result is None:
                        raise ReflowPlanError("reflow_plan_not_ready", "候选尚无成功评估结果")
                    if command.candidate_result_sha256 != content_hash(candidate.result):
                        raise ReflowPlanError(
                            "reflow_plan_result_mismatch", "命令不匹配完整候选结果"
                        )
                    if command.routing_sha256 != source.routing_sha256:
                        raise ReflowPlanError(
                            "reflow_plan_routing_mismatch", "命令没有绑定本次137路由"
                        )
                    if (
                        await current_basis(session, run, self._check(principal)) is not None
                        or incident is None
                        or incident.status != "investigating"
                    ):
                        raise ReflowPlanError(
                            "reflow_plan_source_stale", "候选输入或事故状态已变化"
                        )
                    if incident.version != command.expected_incident_version:
                        raise ReflowPlanError(
                            "reflow_plan_incident_version_conflict", "事故版本已经变化"
                        )
                    if (
                        await session.scalar(
                            select(ReflowPlanRow.id).where(ReflowPlanRow.candidate_job_id == job_id)
                        )
                        is not None
                    ):
                        raise ReflowPlanError(
                            "reflow_plan_already_exists", "该候选已生成另一可信命令绑定的草案"
                        )
                    budget = await read_budget_snapshot(session, run)
                    if any(call.status == "reserved" for call in budget.calls):
                        raise ReflowPlanError("reflow_plan_calls_in_flight", "原运行仍有未结算调用")
                    check_planning_budget(source, packet, budget, now=self._check(principal))
                    content = build_plan(
                        job_id, source, candidate.result, content_hash(candidate.result)
                    )
                    now = self._check(principal)
                    row = ReflowPlanRow(
                        id=uuid5(job_id, "reinvestigation-candidate-plan:v1"),
                        investigation_run_id=run_id,
                        candidate_job_id=job_id,
                        candidate_result_sha256=command.candidate_result_sha256,
                        routing_sha256=command.routing_sha256,
                        incident_version=incident.version,
                        policy_version=POLICY_VERSION,
                        requested_by_id=principal.subject_id,
                        authorization_id=authorization_id,
                        idempotency_key=command.idempotency_key,
                        command_sha256=content_hash(command),
                        content=content.model_dump(mode="json"),
                        content_sha256=content_hash(content),
                        credential_expires_at=principal.expires_at,
                        created_at=now,
                    )
                    session.add(row)
                    await session.flush()
            except (ValueError, CallBudgetExceeded, TaskFactConflict) as error:
                failure = (
                    error
                    if isinstance(error, ReflowPlanError)
                    else ReflowPlanError(
                        "reflow_plan_run_not_found"
                        if isinstance(error, HumanWaitError) and error.status_code == 404
                        else error.code
                        if isinstance(error, CallBudgetExceeded)
                        else "reflow_plan_source_invalid",
                        "候选规划来源或预算无法通过核验",
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
                return ReflowPlanSubmission(error=failure)
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
                plan_id=row.id,
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
                view = await read_plan(session, run, job_id, clock=self._clock)
                check_planning_budget(
                    source,
                    packet,
                    await read_budget_snapshot(session, run),
                    now=self._check(principal),
                )
            self._check(principal)
            return ReflowPlanSubmission(
                result=ReflowPlanSubmissionResult(applied=applied, plan=view)
            )

    async def get(self, run_id: UUID, job_id: UUID) -> ReflowPlanView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            return await read_plan(session, run, job_id, clock=self._clock)

    async def list_commands(
        self, run_id: UUID, job_id: UUID, *, after_cursor: int = 0, limit: int = 50
    ) -> ReflowPlanAuditPage:
        if after_cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("规划审计分页参数不合法")
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            rows = list(
                await session.scalars(
                    select(ReflowPlanCommandRow)
                    .where(
                        ReflowPlanCommandRow.investigation_run_id == run_id,
                        ReflowPlanCommandRow.candidate_job_id == job_id,
                        ReflowPlanCommandRow.cursor > after_cursor,
                    )
                    .order_by(ReflowPlanCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            records = tuple([await validated_audit(session, row) for row in rows[:limit]])
            if any(record.plan_id is not None for record in records):
                await read_plan(session, run, job_id, clock=self._clock)
            return ReflowPlanAuditPage(
                items=records, next_cursor=records[-1].cursor if len(rows) > limit else None
            )


def _record(
    session: AsyncSession,
    run_id: UUID,
    job_id: UUID,
    subject_id: str,
    command: ReflowPlanCommand,
    raw_body: str,
    authorization_id: UUID,
    *,
    now: datetime,
    credential_expires_at: datetime,
    plan_id: UUID | None = None,
    applied: bool = False,
    error: ReflowPlanError | None = None,
) -> None:
    session.add(
        ReflowPlanCommandRow(
            authorization_id=authorization_id,
            investigation_run_id=run_id,
            candidate_job_id=job_id,
            subject_id=subject_id,
            command=command.model_dump(mode="json"),
            raw_body=raw_body,
            command_sha256=content_hash(command),
            outcome="error" if error else "applied" if applied else "replayed",
            http_status=error.status_code if error else 201 if applied else 200,
            plan_id=plan_id,
            error_code=error.code if error else None,
            error_message=str(error) if error else None,
            credential_expires_at=credential_expires_at,
            created_at=now,
        )
    )
