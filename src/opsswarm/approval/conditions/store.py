"""独立核验审批的可信事务、两阶段幂等及只读追溯。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval.conditions.authorization import (
    Command,
    command_stage,
    operation,
    role,
    validate_admission,
)
from opsswarm.approval.conditions.contracts import (
    POLICY_VERSION,
    ApprovalAuditPage,
    ApprovalAvailability,
    ApprovalError,
    DecisionCommand,
    DecisionResult,
    DecisionView,
    RequestCommand,
    RequestResult,
    RequestView,
)
from opsswarm.approval.conditions.policy import (
    build_decision_content,
    build_request_content,
    decision_availability,
    request_availability,
    validate_decision,
    validate_request,
)
from opsswarm.approval.conditions.queries import (
    ApprovalFacts,
    decision_id,
    matches_decision,
    matches_request,
    read_facts,
    request_id,
    validated_audit,
)
from opsswarm.approval.conditions.sources import ApprovalBasis, load_basis, source_availability
from opsswarm.approval.conditions.tables import (
    ConditionApprovalCommandRow,
    ConditionApprovalDecisionRow,
    ConditionApprovalRequestRow,
)
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.candidate.store import utc_now
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict


@dataclass(frozen=True)
class ApprovalSubmission:
    result: RequestResult | DecisionResult | None = None
    error: ApprovalError | None = None


class ConditionApprovalStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock

    def _check(self, principal: OperatorPrincipal, command: Command) -> datetime:
        now = normalized_now(self._clock)
        if now >= principal.expires_at:
            raise OperatorAccessError("operator_credential_expired", "操作者凭据已到期", 401)
        if principal.role != role(command_stage(command)):
            raise OperatorAccessError("operator_permission_denied", "核验审批角色不匹配", 403)
        return now

    async def create_request(
        self,
        run_id: UUID,
        job_id: UUID,
        version: int,
        command: RequestCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> ApprovalSubmission:
        return await self._submit(
            run_id,
            job_id,
            version,
            command,
            principal=principal,
            authorization_id=authorization_id,
            raw_body=raw_body,
        )

    async def decide(
        self,
        run_id: UUID,
        job_id: UUID,
        version: int,
        command: DecisionCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> ApprovalSubmission:
        return await self._submit(
            run_id,
            job_id,
            version,
            command,
            principal=principal,
            authorization_id=authorization_id,
            raw_body=raw_body,
        )

    async def _submit(
        self,
        run_id: UUID,
        job_id: UUID,
        version: int,
        command: Command,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> ApprovalSubmission:
        stage = command_stage(command)
        require_permission(principal, "POST", operation(stage))
        self._check(principal, command)
        if len(raw_body) > 65536:
            raise OperatorAccessError("operator_command_too_large", "核验审批命令超过64 KiB", 413)
        try:
            raw = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "核验审批命令必须为UTF-8", 422
            ) from None
        async with self._factory() as session, session.begin():
            admission = await session.get(
                OperatorAuthorizationRow, authorization_id, with_for_update=True
            )
            admission_time = self._check(principal, command)
            try:
                validate_admission(
                    admission,
                    run_id=run_id,
                    job_id=job_id,
                    version=version,
                    subject_id=principal.subject_id,
                    command=command,
                    raw_body=raw,
                    occurred_at=admission_time,
                )
                if admission is None or admission.credential_id != principal.credential_id:
                    raise ValueError("凭据不一致")
            except ValueError:
                raise OperatorAccessError(
                    "operator_permission_denied", "核验审批缺少同源可信准入", 403
                ) from None
            prior = await session.scalar(
                select(ConditionApprovalCommandRow).where(
                    ConditionApprovalCommandRow.authorization_id == authorization_id
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
                        "operator_permission_denied", "准入已绑定另一核验审批命令", 403
                    )
                if audit.outcome == "error":
                    self._check(principal, command)
                    return ApprovalSubmission(
                        error=ApprovalError(
                            audit.error_code or "condition_approval_conflict",
                            audit.error_message or "核验审批冲突",
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
                self._check(principal, command)
                if isinstance(command, RequestCommand):
                    existing = await session.scalar(
                        select(ConditionApprovalRequestRow).where(
                            ConditionApprovalRequestRow.investigation_run_id == run_id,
                            ConditionApprovalRequestRow.requested_by_id == principal.subject_id,
                            ConditionApprovalRequestRow.idempotency_key == command.idempotency_key,
                        )
                    )
                    matched = existing is not None and matches_request(
                        existing, job_id, version, command
                    )
                else:
                    existing_decision = await session.scalar(
                        select(ConditionApprovalDecisionRow).where(
                            ConditionApprovalDecisionRow.investigation_run_id == run_id,
                            ConditionApprovalDecisionRow.decided_by_id == principal.subject_id,
                            ConditionApprovalDecisionRow.idempotency_key == command.idempotency_key,
                        )
                    )
                    existing = None
                    matched = existing_decision is not None and matches_decision(
                        existing_decision, job_id, version, command
                    )
                has_key = (
                    existing is not None
                    if isinstance(command, RequestCommand)
                    else existing_decision is not None
                )
                if has_key and not matched:
                    raise ApprovalError(
                        "condition_approval_idempotency_conflict",
                        "幂等键已绑定另一完整核验审批命令",
                    )
                # 已存在成功事实后，损坏历史只能拒绝，不能追加更弱的错误审计。
                rollback_only = rollback_only or matched
                has_target = (
                    await session.scalar(
                        select(ConditionApprovalRequestRow.id).where(
                            ConditionApprovalRequestRow.investigation_run_id == run_id,
                            ConditionApprovalRequestRow.candidate_job_id == job_id,
                            ConditionApprovalRequestRow.verification_version == version,
                        )
                    )
                    is not None
                )
                try:
                    basis = await load_basis(session, run, job_id, version, clock=self._clock)
                    facts = await read_facts(session, basis)
                except (ValueError, CallBudgetExceeded, TaskFactConflict):
                    # 换幂等键也不能把损坏的既有审批包装成另一条普通拒绝。
                    rollback_only = rollback_only or has_target
                    raise
                if prior is not None and not matched:
                    raise ApprovalError(
                        "condition_approval_integrity_violation", "成功审计缺少原业务事实"
                    )
                applied = not matched
                if applied:
                    await self._validate_new(
                        session, run, basis, facts, command, self._check(principal, command)
                    )
                    now = self._check(principal, command)
                    rollback_only = True
                    if isinstance(command, RequestCommand):
                        content = build_request_content(basis.verification)
                        row = ConditionApprovalRequestRow(
                            id=request_id(basis.verification.id),
                            investigation_run_id=run_id,
                            candidate_job_id=job_id,
                            verification_id=basis.verification.id,
                            verification_content_sha256=basis.verification.content_sha256,
                            verification_version=version,
                            incident_version=basis.verification.incident_version,
                            policy_version=POLICY_VERSION,
                            status="pending",
                            requested_by_id=principal.subject_id,
                            authorization_id=authorization_id,
                            idempotency_key=command.idempotency_key,
                            command_sha256=content_hash(command),
                            content=content.model_dump(mode="json"),
                            content_sha256=content_hash(content),
                            created_at=now,
                            expires_at=basis.verification.expires_at,
                        )
                        session.add(row)
                    else:
                        assert facts.request is not None
                        decision_content = build_decision_content(facts.request, command.action)
                        session.add(
                            ConditionApprovalDecisionRow(
                                id=decision_id(facts.request.id),
                                investigation_run_id=run_id,
                                candidate_job_id=job_id,
                                request_id=facts.request.id,
                                request_content_sha256=facts.request.content_sha256,
                                verification_id=basis.verification.id,
                                verification_content_sha256=basis.verification.content_sha256,
                                verification_version=version,
                                incident_version=basis.verification.incident_version,
                                policy_version=POLICY_VERSION,
                                outcome=decision_content.outcome,
                                decided_by_id=principal.subject_id,
                                authorization_id=authorization_id,
                                idempotency_key=command.idempotency_key,
                                command_sha256=content_hash(command),
                                content=decision_content.model_dump(mode="json"),
                                content_sha256=content_hash(decision_content),
                                created_at=now,
                            )
                        )
                    await session.flush()
                else:
                    now = self._check(principal, command)
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
                        now=now,
                        applied=applied,
                        request_identifier=request_id(basis.verification.id),
                        decision_identifier=decision_id(request_id(basis.verification.id))
                        if stage == "decision"
                        else None,
                    )
                    await session.flush()
                facts = await read_facts(session, basis)
                view = await self._view(session, run, basis, facts, decision=stage == "decision")
                if applied:
                    # 这里仅复查授权、原期限与当前来源，不再检查刚写入的唯一性。
                    await self._validate_current(
                        session, run, basis, facts, command, self._check(principal, command)
                    )
                self._check(principal, command)
                result: RequestResult | DecisionResult
                if isinstance(view, RequestView):
                    result = RequestResult(applied=applied, request=view)
                else:
                    result = DecisionResult(applied=applied, decision=view)
                return ApprovalSubmission(result=result)
            except OperatorAccessError:
                raise
            except (ValueError, CallBudgetExceeded, TaskFactConflict) as error:
                failure = (
                    error
                    if isinstance(error, ApprovalError)
                    else ApprovalError(
                        "condition_approval_source_invalid",
                        "核验审批来源或预算无法完整重建",
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
                    now=self._check(principal, command),
                    error=failure,
                )
                await session.flush()
                self._check(principal, command)
                return ApprovalSubmission(error=failure)

    async def _validate_new(
        self,
        session: AsyncSession,
        run: InvestigationRunRow,
        basis: ApprovalBasis,
        facts: ApprovalFacts,
        command: Command,
        now: datetime,
    ) -> None:
        await self._validate_current(session, run, basis, facts, command, now)
        if isinstance(command, RequestCommand) and facts.request is not None:
            raise ApprovalError("condition_approval_request_exists", "该核验版本已有不可变审批请求")
        if isinstance(command, DecisionCommand) and facts.decision is not None:
            raise ApprovalError("condition_approval_decision_exists", "该核验审批已有不可变决定")

    async def _validate_current(
        self,
        session: AsyncSession,
        run: InvestigationRunRow,
        basis: ApprovalBasis,
        facts: ApprovalFacts,
        command: Command,
        now: datetime,
    ) -> None:
        availability: ApprovalAvailability = "stale"
        if isinstance(command, RequestCommand) or command.action == "approve":
            availability = await source_availability(session, run, basis, clock=self._clock)
        if isinstance(command, RequestCommand):
            validate_request(command, basis.verification, availability=availability, now=now)
        else:
            if facts.request is None:
                raise ApprovalError(
                    "condition_approval_request_not_found", "核验审批请求不存在", 404
                )
            validate_decision(command, facts.request, availability=availability, now=now)

    async def _view(
        self,
        session: AsyncSession,
        run: InvestigationRunRow,
        basis: ApprovalBasis,
        facts: ApprovalFacts,
        *,
        decision: bool,
    ) -> RequestView | DecisionView:
        if decision:
            if facts.request is None:
                raise ApprovalError(
                    "condition_approval_request_not_found", "核验审批请求不存在", 404
                )
            if facts.decision is None:
                return DecisionView(record=None, availability="pending")
            availability = (
                "recorded"
                if facts.decision.outcome != "approved"
                else await source_availability(session, run, basis, clock=self._clock)
            )
            return DecisionView(
                record=facts.decision,
                availability=decision_availability(facts.decision.outcome, availability),
            )
        if facts.request is None:
            return RequestView(record=None, availability="pending")
        availability = (
            "recorded"
            if facts.decision is not None
            else await source_availability(session, run, basis, clock=self._clock)
        )
        return RequestView(
            record=facts.request,
            availability=request_availability(availability, facts.decision is not None),
        )

    async def get_request(self, run_id: UUID, job_id: UUID, version: int) -> RequestView:
        view = await self._get(run_id, job_id, version, decision=False)
        assert isinstance(view, RequestView)
        return view

    async def get_decision(self, run_id: UUID, job_id: UUID, version: int) -> DecisionView:
        view = await self._get(run_id, job_id, version, decision=True)
        assert isinstance(view, DecisionView)
        return view

    async def _get(
        self, run_id: UUID, job_id: UUID, version: int, *, decision: bool
    ) -> RequestView | DecisionView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            basis = await load_basis(session, run, job_id, version, clock=self._clock)
            return await self._view(
                session, run, basis, await read_facts(session, basis), decision=decision
            )

    async def list_commands(
        self,
        run_id: UUID,
        job_id: UUID,
        version: int,
        *,
        after_cursor: int = 0,
        limit: int = 50,
    ) -> ApprovalAuditPage:
        if after_cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("核验审批审计分页参数不合法")
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            rows = list(
                await session.scalars(
                    select(ConditionApprovalCommandRow)
                    .where(
                        ConditionApprovalCommandRow.investigation_run_id == run_id,
                        ConditionApprovalCommandRow.candidate_job_id == job_id,
                        ConditionApprovalCommandRow.verification_version == version,
                        ConditionApprovalCommandRow.cursor > after_cursor,
                    )
                    .order_by(ConditionApprovalCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            records = tuple([await validated_audit(session, row) for row in rows[:limit]])
            if any(record.outcome != "error" for record in records):
                basis = await load_basis(session, run, job_id, version, clock=self._clock)
                await read_facts(session, basis)
            return ApprovalAuditPage(
                items=records, next_cursor=records[-1].cursor if len(rows) > limit else None
            )


def _record(
    session: AsyncSession,
    run_id: UUID,
    job_id: UUID,
    version: int,
    subject_id: str,
    command: Command,
    raw_body: str,
    authorization_id: UUID,
    *,
    now: datetime,
    request_identifier: UUID | None = None,
    decision_identifier: UUID | None = None,
    applied: bool = False,
    error: ApprovalError | None = None,
) -> None:
    session.add(
        ConditionApprovalCommandRow(
            authorization_id=authorization_id,
            investigation_run_id=run_id,
            candidate_job_id=job_id,
            verification_version=version,
            stage=command_stage(command),
            subject_id=subject_id,
            command=command.model_dump(mode="json"),
            raw_body=raw_body,
            command_sha256=content_hash(command),
            outcome="error" if error else "applied" if applied else "replayed",
            http_status=error.status_code if error else 201 if applied else 200,
            request_id=request_identifier,
            decision_id=decision_identifier,
            error_code=error.code if error else None,
            error_message=str(error) if error else None,
            created_at=now,
        )
    )
