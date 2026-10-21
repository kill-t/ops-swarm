"""以父工作项锁串行化人工命令、可信来源与不可变结果审计。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.handoff.contracts import TerminalHandoffRecord
from opsswarm.investigation.handoff.persistence import validated_handoff
from opsswarm.investigation.handoff.tables import InvestigationTerminalHandoffRow
from opsswarm.investigation.human_wait.authorization import operation_for, validate_admission
from opsswarm.investigation.human_wait.contracts import (
    MAX_COMMAND_BYTES,
    HumanWaitAuditPage,
    HumanWaitError,
    HumanWaitEventRecord,
    HumanWaitResult,
    HumanWaitState,
    WaitCommand,
)
from opsswarm.investigation.human_wait.policy import evaluate_wait_command
from opsswarm.investigation.human_wait.tables import HumanWaitCommandRow, HumanWaitEventRow
from opsswarm.investigation.human_wait.validation import (
    project_wait_state,
    validated_audit,
    validated_wait_events,
)
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.events import append_task_event


@dataclass(frozen=True)
class HumanWaitOutcome:
    result: HumanWaitResult | None = None
    error: HumanWaitError | None = None


class HumanWaitStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime]
    ) -> None:
        self._factory = factory
        self._clock = clock

    def _check_principal(self, principal: OperatorPrincipal) -> datetime:
        now = normalized_now(self._clock)
        if now >= principal.expires_at:
            raise OperatorAccessError("operator_unauthenticated", "操作者认证已经过期")
        return now

    async def get_state(self, run_id: UUID) -> HumanWaitState:
        async with self._factory() as session, session.begin():
            await _locked_run(session, run_id, read=True)
            _, state = await _load_state(session, run_id)
            return state

    async def list_commands(
        self,
        run_id: UUID,
        *,
        after_cursor: int = 0,
        limit: int = 50,
    ) -> HumanWaitAuditPage:
        if after_cursor < 0 or not 1 <= limit <= 100:
            raise ValueError("命令游标或分页大小不合法")
        async with self._factory() as session, session.begin():
            await _locked_run(session, run_id, read=True)
            rows = list(
                await session.scalars(
                    select(HumanWaitCommandRow)
                    .where(
                        HumanWaitCommandRow.investigation_run_id == run_id,
                        HumanWaitCommandRow.cursor > after_cursor,
                    )
                    .order_by(HumanWaitCommandRow.cursor)
                    .limit(limit + 1)
                )
            )
            if any(row.outcome != "error" for row in rows[:limit]):
                await _load_state(session, run_id)
            records = tuple([await validated_audit(session, row) for row in rows[:limit]])
            return HumanWaitAuditPage(
                items=records, next_cursor=records[-1].cursor if len(rows) > limit else None
            )

    async def submit(
        self,
        run_id: UUID,
        command: WaitCommand,
        *,
        principal: OperatorPrincipal,
        authorization_id: UUID,
        raw_body: bytes,
    ) -> HumanWaitOutcome:
        require_permission(principal, "POST", operation_for(command.action))
        now = self._check_principal(principal)
        if len(raw_body) > MAX_COMMAND_BYTES:
            raise OperatorAccessError("operator_command_too_large", "人工命令超过 64 KiB", 413)
        try:
            raw_text = raw_body.decode("utf-8")
        except UnicodeError:
            raise OperatorAccessError(
                "operator_command_invalid", "人工命令必须是 UTF-8", 422
            ) from None
        async with self._factory() as session, session.begin():
            admission = await session.get(
                OperatorAuthorizationRow, authorization_id, with_for_update=True
            )
            now = self._check_principal(principal)
            try:
                validate_admission(
                    admission,
                    run_id=run_id,
                    subject_id=principal.subject_id,
                    command=command,
                    raw_body=raw_text,
                    occurred_at=now,
                )
                if admission is None or admission.credential_id != principal.credential_id:
                    raise ValueError("凭据与准入不一致")
            except ValueError:
                raise OperatorAccessError(
                    "operator_permission_denied", "人工命令缺少同源可信准入", 403
                ) from None

            # 同准入的技术重复调用不再创建第二份业务结果；HTTP 重试会有新的准入。
            prior = await session.scalar(
                select(HumanWaitCommandRow).where(
                    HumanWaitCommandRow.authorization_id == authorization_id
                )
            )
            if prior is not None:
                audit = await validated_audit(session, prior)
                if audit.investigation_run_id != run_id or audit.command_sha256 != content_hash(
                    command
                ):
                    raise OperatorAccessError(
                        "operator_permission_denied", "准入已经绑定另一命令", 403
                    )
                if audit.outcome == "error":
                    self._check_principal(principal)
                    return HumanWaitOutcome(
                        error=HumanWaitError(
                            audit.error_code or "human_wait_conflict",
                            audit.error_message or "人工命令冲突",
                            audit.http_status,
                        )
                    )
                await _locked_run(session, run_id, read=True)
                _, state = await _load_state(session, run_id)
                self._check_principal(principal)
                event = next(item for item in state.events if item.id == audit.event_id)
                return HumanWaitOutcome(result=HumanWaitResult(applied=False, event=event))
            try:
                run = await _locked_run(session, run_id, read=False)
                self._check_principal(principal)
                handoff, state = await _load_state(session, run_id)
                existing = next(
                    (
                        item
                        for item in state.events
                        if item.subject_id == principal.subject_id
                        and item.command.idempotency_key == command.idempotency_key
                    ),
                    None,
                )
                now = self._check_principal(principal)
                action = evaluate_wait_command(
                    handoff,
                    state,
                    command,
                    subject_id=principal.subject_id,
                    existing=existing,
                    now=now,
                )
                if action == "replay":
                    assert existing is not None
                    result = HumanWaitResult(applied=False, event=existing)
                else:
                    incident = await session.scalar(
                        select(IncidentRow)
                        .where(IncidentRow.id == run.incident_id)
                        .with_for_update()
                    )
                    now = self._check_principal(principal)
                    if incident is None or incident.status != "investigating":
                        raise HumanWaitError("human_wait_not_available", "事故不再允许修改人工等待")
                    # 可能等待事故锁；时序和声明观察时间也需使用写入前的实际时间。
                    evaluate_wait_command(
                        handoff,
                        state,
                        command,
                        subject_id=principal.subject_id,
                        existing=None,
                        now=now,
                    )
                    version = state.state_version + 1
                    row = HumanWaitEventRow(
                        id=uuid5(run_id, f"human-wait:{version}"),
                        investigation_run_id=run_id,
                        handoff_id=handoff.id,
                        state_version=version,
                        action=command.action,
                        subject_id=principal.subject_id,
                        authorization_id=authorization_id,
                        idempotency_key=command.idempotency_key,
                        command_sha256=content_hash(command),
                        created_at=now,
                    )
                    session.add(row)
                    await session.flush()
                    if command.action == "cancel":
                        run.status = "cancelled"
                        run.completed_at = run.updated_at = now
                        run.last_error = None
                    event = HumanWaitEventRecord.model_validate(
                        {
                            **{
                                name: getattr(row, name)
                                for name in HumanWaitEventRecord.model_fields
                                if name != "command"
                            },
                            "command": command,
                        }
                    )
                    result = HumanWaitResult(applied=True, event=event)
                _record(
                    session,
                    run_id,
                    principal.subject_id,
                    command,
                    raw_text,
                    authorization_id,
                    now=now,
                    result=result,
                )
                if result.applied:
                    await append_task_event(
                        session,
                        run_id,
                        occurred_at=now,
                        event_type="run.cancelled"
                        if command.action == "cancel"
                        else "run.human_input_saved",
                    )
                await session.flush()
                self._check_principal(principal)
                return HumanWaitOutcome(result=result)
            except (HumanWaitError, TaskFactConflict) as error:
                failure = (
                    error
                    if isinstance(error, HumanWaitError)
                    else HumanWaitError(
                        "human_wait_integrity_violation", "人工等待来源无法通过校验"
                    )
                )
                _record(
                    session,
                    run_id,
                    principal.subject_id,
                    command,
                    raw_text,
                    authorization_id,
                    now=self._check_principal(principal),
                    error=failure,
                )
                await session.flush()
                self._check_principal(principal)
                return HumanWaitOutcome(error=failure)


async def _locked_run(session: AsyncSession, run_id: UUID, *, read: bool) -> InvestigationRunRow:
    work = await session.scalar(
        select(WorkItemRow)
        .where(WorkItemRow.investigation_run_id == run_id)
        .with_for_update(read=read)
    )
    run = await session.get(InvestigationRunRow, run_id)
    if run is None:
        raise HumanWaitError("human_wait_run_not_found", "调查运行不存在", 404)
    if work is None:
        raise HumanWaitError("human_wait_integrity_violation", "调查运行缺少父工作项")
    return run


async def _load_state(
    session: AsyncSession,
    run_id: UUID,
) -> tuple[TerminalHandoffRecord, HumanWaitState]:
    row = await session.scalar(
        select(InvestigationTerminalHandoffRow).where(
            InvestigationTerminalHandoffRow.investigation_run_id == run_id
        )
    )
    if row is None or row.destination != "await_human":
        raise HumanWaitError("human_wait_not_available", "运行未交接至人工等待")
    try:
        handoff = await validated_handoff(session, row)
    except TaskFactConflict as error:
        raise HumanWaitError(
            "human_wait_integrity_violation", "人工等待交接来源校验失败"
        ) from error
    events = await validated_wait_events(session, handoff)
    return handoff, project_wait_state(handoff, events)


def _record(
    session: AsyncSession,
    run_id: UUID,
    subject_id: str,
    command: WaitCommand,
    raw_body: str,
    authorization_id: UUID,
    *,
    now: datetime,
    result: HumanWaitResult | None = None,
    error: HumanWaitError | None = None,
) -> None:
    session.add(
        HumanWaitCommandRow(
            authorization_id=authorization_id,
            investigation_run_id=run_id,
            subject_id=subject_id,
            command=command.model_dump(mode="json"),
            command_sha256=content_hash(command),
            raw_body=raw_body,
            outcome="error" if error else "applied" if result and result.applied else "replayed",
            http_status=error.status_code if error else 201 if result and result.applied else 200,
            event_id=result.event.id if result else None,
            error_code=error.code if error else None,
            error_message=str(error) if error else None,
            created_at=now,
        )
    )
