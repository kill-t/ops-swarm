"""从实际候选成功链重建不可变计划及可信命令。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded, remaining_time
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation_candidate.contracts import (
    ReflowCandidatePacket,
    ReflowCandidateSource,
    ReflowCandidateView,
)
from opsswarm.investigation.reinvestigation_candidate.queries import read_job
from opsswarm.investigation.reinvestigation_candidate.tables import ReflowCandidateJobRow
from opsswarm.investigation.reinvestigation_routing.source import current_basis
from opsswarm.planning.reinvestigation_candidate.authorization import validate_admission
from opsswarm.planning.reinvestigation_candidate.contracts import (
    POLICY_VERSION,
    ReflowPlanAuditRecord,
    ReflowPlanCommand,
    ReflowPlanError,
    ReflowPlanRecord,
    ReflowPlanView,
)
from opsswarm.planning.reinvestigation_candidate.policy import build_plan
from opsswarm.planning.reinvestigation_candidate.tables import ReflowPlanCommandRow, ReflowPlanRow


async def candidate_packet(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> tuple[ReflowCandidateView, ReflowCandidateSource, ReflowCandidatePacket]:
    job = await session.get(ReflowCandidateJobRow, job_id)
    if job_id != run.id or job is None or job.investigation_run_id != run.id:
        raise ReflowPlanError("reflow_plan_job_not_found", "候选任务不存在", 404)
    view = await read_job(session, run, job, clock=clock)
    if job.source is None or job.packet is None:
        raise ReflowPlanError("reflow_plan_not_ready", "回流候选没有完整可用输入")
    if view.result is not None:
        native = await session.scalar(
            text("SELECT reflow_plan_candidate(:id,:at)"), {"id": run.id, "at": clock()}
        )
        if native != view.result.model_dump(mode="json"):
            raise ReflowPlanError("reflow_plan_source_invalid", "138完整成功尝试与来源无法重建")
    # read_job 已完整验证相同不可变来源，解码同一正文避免第二遍递归遍历。
    return (
        view,
        ReflowCandidateSource.model_validate(job.source),
        ReflowCandidatePacket.model_validate(job.packet),
    )


def matches_command(row: ReflowPlanRow, job_id: UUID, command: ReflowPlanCommand) -> bool:
    return (
        row.candidate_job_id == job_id
        and row.candidate_result_sha256 == command.candidate_result_sha256
        and row.routing_sha256 == command.routing_sha256
        and row.incident_version == command.expected_incident_version
        and row.idempotency_key == command.idempotency_key
        and row.command_sha256 == content_hash(command)
    )


async def validated_audit(
    session: AsyncSession, row: ReflowPlanCommandRow
) -> ReflowPlanAuditRecord:
    record = ReflowPlanAuditRecord.model_validate(row, from_attributes=True)
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
        raise ReflowPlanError("reflow_plan_integrity_violation", "规划发生在凭据期限外")
    if row.command != record.command.model_dump(mode="json") or row.command_sha256 != content_hash(
        record.command
    ):
        raise ReflowPlanError("reflow_plan_integrity_violation", "规划命令规范正文或摘要不一致")
    if row.outcome != "error":
        plan = await session.get(ReflowPlanRow, row.plan_id)
        if (
            plan is None
            or plan.investigation_run_id != row.investigation_run_id
            or plan.requested_by_id != row.subject_id
            or not matches_command(plan, row.candidate_job_id, record.command)
            or row.created_at < plan.created_at
            or row.outcome == "applied"
            and (plan.authorization_id != row.authorization_id or plan.created_at != row.created_at)
        ):
            raise ReflowPlanError("reflow_plan_integrity_violation", "规划审计缺少同源计划")
    return record


async def read_plan(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> ReflowPlanView:
    try:
        view, source, _ = await candidate_packet(session, run, job_id, clock=clock)
        row = await session.scalar(
            select(ReflowPlanRow).where(ReflowPlanRow.candidate_job_id == job_id)
        )
        if row is None:
            return ReflowPlanView(record=None, availability="pending")
        record = ReflowPlanRecord.model_validate(row, from_attributes=True)
        creator = await session.scalar(
            select(ReflowPlanCommandRow).where(
                ReflowPlanCommandRow.authorization_id == row.authorization_id
            )
        )
        if (
            view.result is None
            or view.status != "succeeded"
            or row.investigation_run_id != run.id
            or row.id != uuid5(job_id, "reinvestigation-candidate-plan:v1")
            or row.policy_version != POLICY_VERSION
            or row.candidate_result_sha256 != content_hash(view.result)
            or row.routing_sha256 != source.routing_sha256
            or row.created_at < view.result.created_at
            or record.content != build_plan(job_id, source, view.result, content_hash(view.result))
            or row.content != record.content.model_dump(mode="json")
            or row.content_sha256 != content_hash(record.content)
            or row.content_sha256 != raw_content_hash(row.content)
            or row.created_at >= row.credential_expires_at
            or creator is not None
            and creator.credential_expires_at != row.credential_expires_at
            or creator is None
            or creator.outcome != "applied"
            or creator.plan_id != row.id
        ):
            raise ReflowPlanError("reflow_plan_integrity_violation", "计划内容或候选来源无法重建")
        await validated_audit(session, creator)
        incident = await session.get(IncidentRow, run.incident_id)
        stale = (
            await current_basis(session, run, normalized_now(clock)) is not None
            or incident is None
            or incident.version != row.incident_version
        )
        try:
            remaining_time(source.call_budget, source.started_at, normalized_now(clock))
        except CallBudgetExceeded:
            stale = True
        return ReflowPlanView(record=record, availability="stale" if stale else "current")
    except ReflowPlanError:
        raise
    except ValueError as error:
        raise ReflowPlanError(
            "reflow_plan_integrity_violation", "候选计划历史材料无法通过核验"
        ) from error
