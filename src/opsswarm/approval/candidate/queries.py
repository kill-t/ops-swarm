"""重建精确草案、请求、事故迁移事件与独立阶段适用性。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.candidate.authorization import validate_admission
from opsswarm.approval.candidate.contracts import (
    POLICY_VERSION,
    RequestAuditRecord,
    RequestCommand,
    RequestError,
    RequestRecord,
    RequestView,
)
from opsswarm.approval.candidate.policy import request_availability, request_expiry
from opsswarm.approval.candidate.tables import (
    CandidateApprovalCommandRow,
    CandidateApprovalRequestRow,
)
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentEventRow, IncidentRow
from opsswarm.investigation.candidate.contracts import CandidateSource
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.tables import HumanWaitEventRow
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.planning.candidate.contracts import PlanError, PlanRecord
from opsswarm.planning.candidate.queries import read_plan


def matches_command(
    row: CandidateApprovalRequestRow, job_id: UUID, command: RequestCommand
) -> bool:
    return (
        row.candidate_job_id == job_id
        and row.plan_content_sha256 == command.plan_content_sha256
        and row.wait_version == command.expected_wait_version
        and row.previous_incident_version == command.expected_incident_version
        and row.idempotency_key == command.idempotency_key
        and row.command_sha256 == content_hash(command)
    )


async def validated_audit(
    session: AsyncSession, row: CandidateApprovalCommandRow
) -> RequestAuditRecord:
    record = RequestAuditRecord.model_validate(row, from_attributes=True)
    validate_admission(
        await session.get(OperatorAuthorizationRow, row.authorization_id),
        run_id=row.investigation_run_id,
        job_id=row.candidate_job_id,
        subject_id=row.subject_id,
        command=record.command,
        raw_body=row.raw_body,
        occurred_at=row.created_at,
    )
    if row.command != record.command.model_dump(mode="json") or row.command_sha256 != content_hash(
        record.command
    ):
        raise RequestError("candidate_approval_integrity_violation", "审批请求命令或摘要不一致")
    if row.outcome != "error":
        request = await session.get(CandidateApprovalRequestRow, row.request_id)
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
            )
        ):
            raise RequestError("candidate_approval_integrity_violation", "审批请求审计缺少同源事实")
    return record


async def plan_source(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> tuple[PlanRecord, CandidateSource, str]:
    try:
        view = await read_plan(session, run, job_id, clock=clock)
    except PlanError as error:
        raise RequestError(
            "candidate_approval_source_invalid", "候选草案来源无法通过核验", error.status_code
        ) from error
    if view.record is None:
        raise RequestError("candidate_approval_plan_not_found", "候选尚无可信计划草案", 404)
    job = await session.get(CandidateJobRow, job_id)
    assert job is not None
    return view.record, CandidateSource.model_validate(job.source), view.availability


async def read_request(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> RequestView:
    try:
        plan, source, availability = await plan_source(session, run, job_id, clock=clock)
        row = await session.scalar(
            select(CandidateApprovalRequestRow).where(
                CandidateApprovalRequestRow.candidate_job_id == job_id
            )
        )
        if row is None:
            return RequestView(
                record=None, availability="pending" if availability == "current" else "stale"
            )
        record = RequestRecord.model_validate(row, from_attributes=True)
        creator = await session.scalar(
            select(CandidateApprovalCommandRow).where(
                CandidateApprovalCommandRow.authorization_id == row.authorization_id
            )
        )
        event = await session.get(IncidentEventRow, row.event_cursor)
        if (
            row.investigation_run_id != run.id
            or row.plan_id != plan.id
            or row.id != uuid5(plan.id, "candidate-approval-request:v1")
            or row.policy_version != POLICY_VERSION
            or row.status != "pending"
            or row.plan_content_sha256 != plan.content_sha256
            or row.wait_version != plan.wait_version
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
            raise RequestError(
                "candidate_approval_integrity_violation", "审批请求无法绑定精确草案与来源"
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
            raise RequestError(
                "candidate_approval_integrity_violation", "审批请求缺少精确事故迁移事件"
            )
        incident = await session.get(IncidentRow, run.incident_id)
        latest = await session.scalar(
            select(HumanWaitEventRow)
            .where(HumanWaitEventRow.investigation_run_id == run.id)
            .order_by(HumanWaitEventRow.state_version.desc())
            .limit(1)
        )
        return RequestView(
            record=record,
            availability=request_availability(
                record,
                source_kind=source.source_kind,
                run_status=run.status,
                latest_wait_version=latest.state_version if latest else None,
                wait_cancelled=latest is not None and latest.action == "cancel",
                incident_status=incident.status if incident else None,
                incident_version=incident.version if incident else None,
                now=normalized_now(clock),
                run_deadline=source.started_at
                + timedelta(seconds=source.call_budget.max_duration_seconds),
            ),
        )
    except RequestError:
        raise
    except ValueError as error:
        raise RequestError(
            "candidate_approval_integrity_violation", "审批请求历史材料无法通过核验"
        ) from error


async def permits_wait_input(
    session: AsyncSession,
    run: InvestigationRunRow,
    incident: IncidentRow,
    *,
    clock: Callable[[], datetime],
) -> bool:
    """待审批期间仍允许可信补充或取消，使旧依据失效而非锁死人工入口。"""
    if incident.status != "awaiting_approval" or run.status != "await_human":
        return False
    row = await session.scalar(
        select(CandidateApprovalRequestRow).where(
            CandidateApprovalRequestRow.investigation_run_id == run.id,
            CandidateApprovalRequestRow.incident_version == incident.version,
        )
    )
    if row is None or row.wait_version is None:
        return False
    # 历史仍须完整可重建；补充后可继续补充，失效请求不能因此重新变为可批准。
    try:
        view = await read_request(session, run, row.candidate_job_id, clock=clock)
    except RequestError:
        return False
    return view.record is not None and view.record.incident_version == incident.version
