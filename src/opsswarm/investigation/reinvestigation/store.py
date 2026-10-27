"""可信回流请求的唯一准入、精确重放与逐请求业务审计。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.execution.release.observer import utc_now
from opsswarm.execution.release_jobs.store import lock_parent
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.convergence.contracts import ConvergenceError
from opsswarm.incidents.convergence.queries import read_convergence
from opsswarm.incidents.convergence.tables import CandidateIncidentConvergenceRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.reinvestigation.authorization import check_principal, validate_admission
from opsswarm.investigation.reinvestigation.contracts import (
    OPERATION,
    ReinvestigationAuditPage,
    ReinvestigationCommand,
    ReinvestigationContent,
    ReinvestigationError,
    ReinvestigationSubmission,
    ReinvestigationView,
)
from opsswarm.investigation.reinvestigation.policy import budget_blockers, make_content
from opsswarm.investigation.reinvestigation.queries import has_new_alert, read_audit, read_view
from opsswarm.investigation.reinvestigation.tables import (
    CandidateReinvestigationCommandRow,
    CandidateReinvestigationRow,
)
from opsswarm.investigation.settlement.tables import CandidateSettlementRow


@dataclass(frozen=True)
class ReinvestigationOutcome:
    result: ReinvestigationSubmission | None = None
    error: ReinvestigationError | None = None


class CandidateReinvestigationStore:
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
        command: ReinvestigationCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> ReinvestigationOutcome:
        require_permission(principal, "POST", OPERATION)
        self._check(principal)
        if len(raw_body) > 65536:
            raise OperatorAccessError("operator_command_too_large", "回流命令超过64 KiB", 413)
        try:
            raw = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "回流命令必须为UTF-8", 422
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
                    raise ValueError("可信凭据不一致")
            except ValueError:
                raise OperatorAccessError(
                    "operator_permission_denied", "回流命令缺少同源可信准入", 403
                ) from None
            prior_audit = await session.scalar(
                select(CandidateReinvestigationCommandRow).where(
                    CandidateReinvestigationCommandRow.authorization_id == authorization_id
                )
            )
            if prior_audit is not None:
                audit = await read_audit(session, prior_audit)
                if (
                    audit.investigation_run_id != run_id
                    or audit.subject_id != principal.subject_id
                    or audit.command != command
                ):
                    raise OperatorAccessError(
                        "operator_permission_denied", "授权已经绑定另一命令", 403
                    )
                if audit.outcome == "error":
                    self._check(principal)
                    return ReinvestigationOutcome(
                        error=ReinvestigationError(
                            audit.error_code or "reinvestigation_conflict",
                            audit.error_message or "回流冲突",
                            audit.http_status,
                        )
                    )
                run = await lock_wait_run(session, run_id, read=True)
                row = await session.get(CandidateReinvestigationRow, audit.request_id)
                view = await read_view(session, run, row, clock=self._clock)
                self._check(principal)
                return ReinvestigationOutcome(
                    result=ReinvestigationSubmission(applied=False, request=view)
                )
            mutated = False
            try:
                try:
                    run = await lock_wait_run(session, run_id, read=False)
                except HumanWaitError as error:
                    if error.status_code != 404:
                        raise
                    raise ReinvestigationError(
                        "reinvestigation_run_not_found", "调查运行不存在", 404
                    ) from error
                settlement = await session.scalar(
                    select(CandidateSettlementRow).where(
                        CandidateSettlementRow.investigation_run_id == run_id
                    )
                )
                if settlement is not None:
                    run = await lock_parent(session, run_id, settlement.candidate_job_id)
                now = self._check(principal)
                row = await session.scalar(
                    select(CandidateReinvestigationRow).where(
                        CandidateReinvestigationRow.investigation_run_id == run_id
                    )
                )
                applied = row is None
                if row is not None:
                    view = await read_view(session, run, row, clock=self._clock)
                    stored = ReinvestigationContent.model_validate(row.content)
                    if stored.subject_id != principal.subject_id or (
                        stored.command.idempotency_key != command.idempotency_key
                    ):
                        raise ReinvestigationError(
                            "reinvestigation_already_exists", "本根运行已使用唯一回流准入次数"
                        )
                    if stored.command != command:
                        raise ReinvestigationError(
                            "reinvestigation_idempotency_conflict", "幂等键已经绑定另一完整回流命令"
                        )
                else:
                    source = await session.get(CandidateIncidentConvergenceRow, run_id)
                    if source is None:
                        raise ReinvestigationError(
                            "reinvestigation_source_not_found", "运行尚无候选事故收敛决定", 404
                        )
                    try:
                        convergence = await read_convergence(
                            session, run, source, clock=self._clock
                        )
                    except ConvergenceError as error:
                        raise ReinvestigationError(
                            "reinvestigation_integrity_violation", "回流收敛来源无法完整重建"
                        ) from error
                    now = self._check(principal)
                    if (
                        convergence.outcome != "observed_failure"
                        or run.status != "settled"
                        or run.candidate_slot_released_at is not None
                    ):
                        raise ReinvestigationError(
                            "reinvestigation_source_not_eligible", "只有持槽的明确验证失败允许回流"
                        )
                    incident = await session.get(IncidentRow, run.incident_id)
                    if (
                        incident is None
                        or incident.status != "investigating"
                        or incident.version != convergence.after_version
                        or await has_new_alert(
                            session, run.id, run.incident_id, convergence.after_version
                        )
                    ):
                        raise ReinvestigationError(
                            "reinvestigation_basis_changed", "事故版本或告警依据已变化"
                        )
                    budget = await read_budget_snapshot(session, run)
                    now = self._check(principal)
                    content = make_content(
                        run_id=run_id,
                        incident_id=run.incident_id,
                        convergence=convergence,
                        budget=budget,
                        command=command,
                        subject_id=principal.subject_id,
                        authorization_id=authorization_id,
                        now=now,
                    )
                    mutated = True
                    row = CandidateReinvestigationRow(
                        id=content.id,
                        investigation_run_id=run_id,
                        incident_id=run.incident_id,
                        authorization_id=authorization_id,
                        content=content.model_dump(mode="json"),
                        content_sha256=content_hash(content),
                        created_at=now,
                    )
                    session.add(row)
                    await session.flush()
            except ReinvestigationError as error:
                if mutated or error.code == "reinvestigation_integrity_violation":
                    raise
                self._check(principal)
                _record(
                    session,
                    run_id,
                    principal,
                    command,
                    raw,
                    authorization_id,
                    # 失败原因绑定判定时刻，不能在写审计时因期限跨越而变成另一原因。
                    now=now,
                    error=error,
                )
                await session.flush()
                self._check(principal)
                return ReinvestigationOutcome(error=error)
            assert row is not None
            _record(
                session,
                run_id,
                principal,
                command,
                raw,
                authorization_id,
                now=now if applied else self._check(principal),
                request_id=row.id,
                applied=applied,
            )
            await session.flush()
            view = await read_view(session, run, row, clock=self._clock)
            checked_at = self._check(principal)
            if applied:
                blockers = budget_blockers(await read_budget_snapshot(session, run), now=checked_at)
                if blockers:
                    raise ReinvestigationError(blockers[0], "写后原预算或期限不再允许回流")
            return ReinvestigationOutcome(
                result=ReinvestigationSubmission(applied=applied, request=view)
            )

    async def get(self, run_id: UUID) -> ReinvestigationView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            row = await session.scalar(
                select(CandidateReinvestigationRow).where(
                    CandidateReinvestigationRow.investigation_run_id == run_id
                )
            )
            return await read_view(session, run, row, clock=self._clock)

    async def list_commands(
        self, run_id: UUID, *, after_cursor: int = 0, limit: int = 50
    ) -> ReinvestigationAuditPage:
        if after_cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("审计分页参数不合法")
        async with self._factory() as session, session.begin():
            rows = list(
                await session.scalars(
                    select(CandidateReinvestigationCommandRow)
                    .where(
                        CandidateReinvestigationCommandRow.investigation_run_id == run_id,
                        CandidateReinvestigationCommandRow.cursor > after_cursor,
                    )
                    .order_by(CandidateReinvestigationCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            items = tuple([await read_audit(session, row) for row in rows[:limit]])
            if any(item.request_id is not None for item in items):
                run = await lock_wait_run(session, run_id, read=True)
                row = await session.scalar(
                    select(CandidateReinvestigationRow).where(
                        CandidateReinvestigationRow.investigation_run_id == run_id
                    )
                )
                await read_view(session, run, row, clock=self._clock)
            return ReinvestigationAuditPage(
                items=items, next_cursor=items[-1].cursor if len(rows) > limit else None
            )


def _record(
    session: AsyncSession,
    run_id: UUID,
    principal: OperatorPrincipal,
    command: ReinvestigationCommand,
    raw: str,
    authorization_id: UUID,
    *,
    now: datetime,
    request_id: UUID | None = None,
    applied: bool = False,
    error: ReinvestigationError | None = None,
) -> None:
    session.add(
        CandidateReinvestigationCommandRow(
            authorization_id=authorization_id,
            investigation_run_id=run_id,
            subject_id=principal.subject_id,
            command=command.model_dump(mode="json"),
            raw_body=raw,
            command_sha256=content_hash(command),
            outcome="error" if error else "applied" if applied else "replayed",
            http_status=error.status_code if error else 201 if applied else 200,
            request_id=request_id,
            error_code=error.code if error else None,
            error_message=str(error) if error else None,
            created_at=now,
        )
    )
