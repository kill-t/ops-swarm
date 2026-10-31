"""重建精确草案、请求、事故迁移事件与独立阶段适用性。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.reinvestigation_candidate.authorization import validate_admission
from opsswarm.approval.reinvestigation_candidate.contracts import (
    POLICY_VERSION,
    ReflowRequestAuditRecord,
    ReflowRequestCommand,
    ReflowRequestError,
    ReflowRequestRecord,
    ReflowRequestView,
)
from opsswarm.approval.reinvestigation_candidate.policy import request_availability, request_expiry
from opsswarm.approval.reinvestigation_candidate.tables import (
    ReflowApprovalCommandRow,
    ReflowApprovalRequestRow,
)
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentEventRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation_candidate.contracts import ReflowCandidateSource
from opsswarm.investigation.reinvestigation_candidate.tables import ReflowCandidateJobRow
from opsswarm.planning.reinvestigation_candidate.contracts import ReflowPlanError, ReflowPlanRecord
from opsswarm.planning.reinvestigation_candidate.queries import read_plan


def matches_command(
    row: ReflowApprovalRequestRow, job_id: UUID, command: ReflowRequestCommand
) -> bool:
    return (
        row.candidate_job_id == job_id
        and row.plan_content_sha256 == command.plan_content_sha256
        and row.previous_incident_version == command.expected_incident_version
        and row.idempotency_key == command.idempotency_key
        and row.command_sha256 == content_hash(command)
    )


async def validated_audit(
    session: AsyncSession, row: ReflowApprovalCommandRow
) -> ReflowRequestAuditRecord:
    record = ReflowRequestAuditRecord.model_validate(row, from_attributes=True)
    validate_admission(
        await session.get(OperatorAuthorizationRow, row.authorization_id),
        run_id=row.investigation_run_id,
        job_id=row.candidate_job_id,
        subject_id=row.subject_id,
        command=record.command,
        raw_body=row.raw_body,
        occurred_at=row.created_at,
    )
    if row.created_at >= row.credential_expires_at:
        raise ReflowRequestError("reflow_approval_integrity_violation", "命令发生在凭据期限外")
    if row.command != record.command.model_dump(mode="json") or row.command_sha256 != content_hash(
        record.command
    ):
        raise ReflowRequestError("reflow_approval_integrity_violation", "审批请求命令或摘要不一致")
    if row.outcome != "error":
        request = await session.get(ReflowApprovalRequestRow, row.request_id)
        if (
            request is None
            or request.investigation_run_id != row.investigation_run_id
            or request.requested_by_id != row.subject_id
            or not matches_command(request, row.candidate_job_id, record.command)
            or row.created_at < request.created_at
            or row.outcome == "applied"
            and (
                request.authorization_id != row.authorization_id
                or request.created_at != row.created_at
                or request.credential_expires_at != row.credential_expires_at
            )
        ):
            raise ReflowRequestError(
                "reflow_approval_integrity_violation", "审批请求审计缺少同源事实"
            )
    return record


async def plan_source(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> tuple[ReflowPlanRecord, ReflowCandidateSource, str]:
    try:
        view = await read_plan(session, run, job_id, clock=clock)
    except ReflowPlanError as error:
        raise ReflowRequestError(
            "reflow_approval_source_invalid", "候选草案来源无法通过核验", error.status_code
        ) from error
    if view.record is None:
        raise ReflowRequestError("reflow_approval_plan_not_found", "候选尚无可信计划草案", 404)
    job = await session.get(ReflowCandidateJobRow, job_id)
    assert job is not None
    return view.record, ReflowCandidateSource.model_validate(job.source), view.availability


async def read_request(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> ReflowRequestView:
    try:
        plan, source, availability = await plan_source(session, run, job_id, clock=clock)
        row = await session.scalar(
            select(ReflowApprovalRequestRow).where(
                ReflowApprovalRequestRow.candidate_job_id == job_id
            )
        )
        if row is None:
            return ReflowRequestView(
                record=None, availability="pending" if availability == "current" else "stale"
            )
        record = ReflowRequestRecord.model_validate(row, from_attributes=True)
        creator = await session.scalar(
            select(ReflowApprovalCommandRow).where(
                ReflowApprovalCommandRow.authorization_id == row.authorization_id
            )
        )
        event = await session.get(IncidentEventRow, row.event_cursor)
        if (
            row.investigation_run_id != run.id
            or row.plan_id != plan.id
            or row.id != uuid5(plan.id, "reinvestigation-approval-request:v1")
            or row.policy_version != POLICY_VERSION
            or row.status != "pending"
            or row.plan_content_sha256 != plan.content_sha256
            or row.basis_event_cursor >= row.event_cursor
            or row.created_at >= row.credential_expires_at
            or row.previous_incident_version != plan.incident_version
            or row.incident_version != row.previous_incident_version + 1
            or row.created_at < plan.created_at
            or row.expires_at
            != request_expiry(
                started_at=source.started_at,
                max_duration_seconds=source.call_budget.max_duration_seconds,
                now=row.created_at,
            )
            or creator is None
            or creator.outcome != "applied"
            or creator.request_id != row.id
        ):
            raise ReflowRequestError(
                "reflow_approval_integrity_violation", "审批请求无法绑定精确草案与来源"
            )
        audit = await validated_audit(session, creator)
        if (
            event is None
            or event.incident_id != run.incident_id
            or event.incident_version != row.incident_version
            or event.event_type != "incident.status_changed"
            or event.actor_type != "human"
            or event.actor_id != row.requested_by_id
            or event.from_status != "investigating"
            or event.to_status != "awaiting_approval"
            or event.payload != {"action": "request_approval"}
            or event.reason != audit.command.reason
            or event.occurred_at != row.created_at
        ):
            raise ReflowRequestError(
                "reflow_approval_integrity_violation", "审批请求缺少精确事故迁移事件"
            )
        # 历史闭包与当前审批阶段分别校验，不套用旧investigating阶段的current。
        await session.execute(
            text(
                "SELECT assert_reflow_approval_request(r,false) "
                "FROM reinvestigation_approval_requests r WHERE id=:id"
            ),
            {"id": row.id},
        )
        blocker = await session.scalar(
            text(
                "SELECT reflow_approval_basis_blocker(r,:at) "
                "FROM reinvestigation_approval_requests r WHERE id=:id"
            ),
            {"id": row.id, "at": normalized_now(clock)},
        )
        if blocker not in (
            None,
            "reflow_source_changed",
            "run_deadline_exceeded",
            "reflow_approval_expired",
        ):
            raise ReflowRequestError("reflow_approval_integrity_violation", "未知审批阶段阻断")
        return ReflowRequestView(
            record=record,
            availability=request_availability(
                record,
                basis_current=blocker is None,
                now=normalized_now(clock),
                run_deadline=source.started_at
                + timedelta(seconds=source.call_budget.max_duration_seconds),
            ),
        )
    except ReflowRequestError:
        raise
    except ValueError as error:
        raise ReflowRequestError(
            "reflow_approval_integrity_violation", "审批请求历史材料无法通过核验"
        ) from error
