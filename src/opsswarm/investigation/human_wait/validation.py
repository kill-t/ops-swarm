"""从交接、连续人工事件与实际授权命令重建有界等待事实。"""

from uuid import uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.handoff.contracts import TerminalHandoffRecord
from opsswarm.investigation.human_wait.authorization import parse_command, validate_admission
from opsswarm.investigation.human_wait.contracts import (
    MAX_WAIT_EVENTS,
    HumanWaitAuditRecord,
    HumanWaitError,
    HumanWaitEventRecord,
    HumanWaitState,
)
from opsswarm.investigation.human_wait.policy import evaluate_wait_command
from opsswarm.investigation.human_wait.tables import HumanWaitCommandRow, HumanWaitEventRow


async def validated_audit(session: AsyncSession, row: HumanWaitCommandRow) -> HumanWaitAuditRecord:
    try:
        command = parse_command(row.command)
        record = HumanWaitAuditRecord.model_validate(
            {
                **{
                    name: getattr(row, name)
                    for name in HumanWaitAuditRecord.model_fields
                    if name != "command"
                },
                "command": command,
            }
        )
        admission = await session.get(OperatorAuthorizationRow, row.authorization_id)
        validate_admission(
            admission,
            run_id=row.investigation_run_id,
            subject_id=row.subject_id,
            command=command,
            raw_body=row.raw_body,
            occurred_at=row.created_at,
        )
        if content_hash(command) != row.command_sha256 or row.command != command.model_dump(
            mode="json"
        ):
            raise ValueError("规范命令散列不一致")
        if row.outcome != "error":
            event = await session.get(HumanWaitEventRow, row.event_id)
            if (
                event is None
                or event.investigation_run_id != row.investigation_run_id
                or event.subject_id != row.subject_id
                or event.command_sha256 != row.command_sha256
                or event.idempotency_key != command.idempotency_key
                or event.created_at > row.created_at
                or (
                    row.outcome == "applied"
                    and (
                        event.authorization_id != row.authorization_id
                        or event.created_at != row.created_at
                    )
                )
            ):
                raise ValueError("业务结果没有匹配的人工事件")
        return record
    except ValueError as error:
        raise HumanWaitError(
            "human_wait_integrity_violation", "人工命令业务结果无法通过来源校验"
        ) from error


async def validated_wait_events(
    session: AsyncSession,
    handoff: TerminalHandoffRecord,
) -> tuple[HumanWaitEventRecord, ...]:
    """不调用交接查询，避免取消后的历史交接验证形成递归。"""

    rows = list(
        await session.scalars(
            select(HumanWaitEventRow)
            .where(HumanWaitEventRow.investigation_run_id == handoff.investigation_run_id)
            .order_by(HumanWaitEventRow.state_version)
            .limit(MAX_WAIT_EVENTS + 1)
        )
    )
    if len(rows) > MAX_WAIT_EVENTS:
        raise HumanWaitError("human_wait_integrity_violation", "人工事件超过有界上限")
    events: list[HumanWaitEventRecord] = []
    try:
        for index, row in enumerate(rows):
            audit_row = await session.scalar(
                select(HumanWaitCommandRow).where(
                    HumanWaitCommandRow.authorization_id == row.authorization_id
                )
            )
            if audit_row is None or audit_row.outcome != "applied":
                raise ValueError("人工事件缺少实际应用命令")
            audit = await validated_audit(session, audit_row)
            command = audit.command
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
            state = project_wait_state(handoff, tuple(events))
            if (
                row.id != uuid5(handoff.investigation_run_id, f"human-wait:{index + 2}")
                or row.state_version != index + 2
                or row.handoff_id != handoff.id
                or row.action != command.action
                or row.idempotency_key != command.idempotency_key
                or row.command_sha256 != content_hash(command)
                or evaluate_wait_command(
                    handoff,
                    state,
                    command,
                    subject_id=row.subject_id,
                    existing=None,
                    now=row.created_at,
                )
                != "apply"
            ):
                raise ValueError("人工事件与来源或连续等待版本不匹配")
            events.append(event)
    except ValueError as error:
        raise HumanWaitError(
            "human_wait_integrity_violation", "人工等待事件无法通过来源校验"
        ) from error
    return tuple(events)


def project_wait_state(
    handoff: TerminalHandoffRecord,
    events: tuple[HumanWaitEventRecord, ...],
) -> HumanWaitState:
    return HumanWaitState(
        investigation_run_id=handoff.investigation_run_id,
        handoff_id=handoff.id,
        source_sha256=handoff.source_sha256,
        state_version=len(events) + 1,
        status="cancelled" if events and events[-1].action == "cancel" else "waiting",
        events=events,
    )
