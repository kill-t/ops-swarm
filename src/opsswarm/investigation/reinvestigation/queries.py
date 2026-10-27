"""按原来源和账本前缀重建准入历史，当前诊断不改写历史。"""

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.convergence.queries import read_convergence
from opsswarm.incidents.convergence.tables import CandidateIncidentConvergenceRow
from opsswarm.incidents.persistence.tables import IncidentEventRow, IncidentRow
from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.model_policy import summarize_model_usage
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation.authorization import validate_admission
from opsswarm.investigation.reinvestigation.contracts import (
    ReinvestigationAudit,
    ReinvestigationContent,
    ReinvestigationError,
    ReinvestigationRecord,
    ReinvestigationView,
)
from opsswarm.investigation.reinvestigation.policy import budget_blockers, make_content
from opsswarm.investigation.reinvestigation.tables import (
    CandidateReinvestigationCommandRow,
    CandidateReinvestigationRow,
)


async def has_new_alert(
    session: AsyncSession, run_id: UUID, incident_id: UUID, after_version: int
) -> bool:
    anchor = await session.scalar(
        select(IncidentEventRow.id).where(
            IncidentEventRow.incident_id == incident_id,
            IncidentEventRow.incident_version == after_version,
            IncidentEventRow.event_type == "incident.status_changed",
            IncidentEventRow.actor_id == f"candidate-convergence:{run_id}",
        )
    )
    if anchor is None:
        raise ReinvestigationError("reinvestigation_integrity_violation", "回流缺少事故收敛事件")
    return (
        await session.scalar(
            select(IncidentEventRow.id)
            .where(
                IncidentEventRow.incident_id == incident_id,
                IncidentEventRow.event_type == "incident.alert_attached",
                IncidentEventRow.id > anchor,
            )
            .limit(1)
        )
        is not None
    )


async def read_audit(
    session: AsyncSession, row: CandidateReinvestigationCommandRow
) -> ReinvestigationAudit:
    try:
        audit = ReinvestigationAudit.model_validate(row, from_attributes=True)
        admission = await session.get(OperatorAuthorizationRow, row.authorization_id)
        validate_admission(
            admission,
            run_id=audit.investigation_run_id,
            subject_id=audit.subject_id,
            command=audit.command,
            raw_body=row.raw_body,
            occurred_at=audit.created_at,
        )
        if row.command_sha256 != content_hash(
            audit.command
        ) or row.command != audit.command.model_dump(mode="json"):
            raise ValueError("审计命令内容或摘要不一致")
        if audit.outcome == "error":
            if (
                audit.request_id is not None
                or audit.http_status not in (404, 409)
                or not audit.error_code
                or not audit.error_message
            ):
                raise ValueError("失败审计字段不一致")
        else:
            request = await session.get(CandidateReinvestigationRow, audit.request_id)
            if request is None:
                raise ValueError("成功审计缺少请求")
            content = ReinvestigationContent.model_validate(request.content)
            if (
                content.investigation_run_id != audit.investigation_run_id
                or content.subject_id != audit.subject_id
                or content.command != audit.command
                or audit.created_at < content.created_at
                or audit.error_code is not None
                or audit.error_message is not None
                or audit.http_status != (201 if audit.outcome == "applied" else 200)
                or audit.outcome == "applied"
                and (
                    content.authorization_id != audit.authorization_id
                    or content.created_at != audit.created_at
                )
            ):
                raise ValueError("成功审计不匹配原请求")
        return audit
    except (ValueError, TypeError) as error:
        raise ReinvestigationError(
            "reinvestigation_integrity_violation", "回流命令审计无法重建"
        ) from error


def historical_budget(
    current: CallBudgetSnapshot, captured: CallBudgetSnapshot, created_at: datetime
) -> CallBudgetSnapshot:
    end = len(captured.calls)
    if captured.calls != current.calls[:end] or any(
        call.reserved_at <= created_at for call in current.calls[end:]
    ):
        raise ReinvestigationError("reinvestigation_integrity_violation", "原账本历史前缀不一致")
    if current.model_budget is None:
        raise ReinvestigationError("reinvestigation_integrity_violation", "原模型预算缺失")
    return current.model_copy(
        update={
            "calls": current.calls[:end],
            "reserved_model_calls": sum(c.kind == "model" for c in captured.calls),
            "reserved_tool_calls": sum(c.kind == "tool" for c in captured.calls),
            "model_usage": summarize_model_usage(
                current.model_budget,
                [
                    (c.model_allocation, c.model_usage)
                    for c in captured.calls
                    if c.model_allocation is not None
                ],
            ),
        }
    )


async def read_view(
    session: AsyncSession,
    run: InvestigationRunRow,
    row: CandidateReinvestigationRow | None,
    *,
    clock: Callable[[], datetime],
) -> ReinvestigationView:
    if row is None:
        return ReinvestigationView(record=None, availability="pending", blocking_reason_codes=())
    try:
        content = ReinvestigationContent.model_validate(row.content)
        convergence_row = await session.get(CandidateIncidentConvergenceRow, run.id)
        if convergence_row is None:
            raise ValueError("回流缺少收敛来源")
        convergence = await read_convergence(session, run, convergence_row, clock=clock)
        budget = await read_budget_snapshot(session, run)
        captured = historical_budget(budget, content.budget, content.created_at)
        expected = make_content(
            run_id=run.id,
            incident_id=run.incident_id,
            convergence=convergence,
            budget=captured,
            command=content.command,
            subject_id=content.subject_id,
            authorization_id=content.authorization_id,
            now=content.created_at,
        )
        if (
            content != expected
            or row.id != content.id
            or row.investigation_run_id != run.id
            or row.incident_id != run.incident_id
            or row.authorization_id != content.authorization_id
            or row.created_at != content.created_at
            or row.content_sha256 != content_hash(content)
            or row.content_sha256 != raw_content_hash(row.content)
        ):
            raise ValueError("请求与确定性来源或摘要不一致")
        applied = list(
            await session.scalars(
                select(CandidateReinvestigationCommandRow).where(
                    CandidateReinvestigationCommandRow.request_id == row.id,
                    CandidateReinvestigationCommandRow.outcome == "applied",
                )
            )
        )
        if len(applied) != 1:
            raise ValueError("请求缺少唯一成功审计")
        await read_audit(session, applied[0])
        incident = await session.get(IncidentRow, run.incident_id)
        blockers = budget_blockers(budget, now=normalized_now(clock))
        availability: Literal["stale", "expired", "blocked", "current"]
        if (
            incident is None
            or incident.status != "investigating"
            or incident.version != content.incident_version
        ):
            availability, blockers = "stale", ("incident_basis_changed", *blockers)
        elif await has_new_alert(session, run.id, run.incident_id, content.incident_version):
            availability, blockers = "stale", ("new_alert_after_convergence", *blockers)
        elif "run_deadline_exceeded" in blockers:
            availability = "expired"
        else:
            availability = "blocked" if blockers else "current"
        return ReinvestigationView(
            record=ReinvestigationRecord(**content.model_dump(), content_sha256=row.content_sha256),
            availability=availability,
            blocking_reason_codes=blockers,
        )
    except (ValueError, TypeError) as error:
        raise ReinvestigationError(
            "reinvestigation_integrity_violation", "回流准入历史无法重建"
        ) from error
