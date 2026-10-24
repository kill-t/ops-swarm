"""从实际候选成功链重建不可变计划及可信命令。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.policy import CallBudgetExceeded, remaining_time
from opsswarm.investigation.candidate.contracts import (
    CandidateJobView,
    CandidatePacket,
    CandidateSource,
)
from opsswarm.investigation.candidate.queries import read_job
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.planning.candidate.authorization import validate_admission
from opsswarm.planning.candidate.contracts import (
    POLICY_VERSION,
    PlanAuditRecord,
    PlanCommand,
    PlanError,
    PlanRecord,
    PlanView,
)
from opsswarm.planning.candidate.policy import build_plan
from opsswarm.planning.candidate.tables import CandidatePlanCommandRow, CandidatePlanRow


async def candidate_packet(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> tuple[CandidateJobView, CandidateSource, CandidatePacket]:
    job = await session.get(CandidateJobRow, job_id)
    if job is None or job.investigation_run_id != run.id:
        raise PlanError("candidate_plan_job_not_found", "候选任务不存在", 404)
    view = await read_job(session, job, run, clock=clock)
    # read_job 已完整验证相同不可变来源，解码同一正文避免第二遍递归遍历。
    return (
        view,
        CandidateSource.model_validate(job.source),
        CandidatePacket.model_validate(job.packet),
    )


def matches_command(row: CandidatePlanRow, job_id: UUID, command: PlanCommand) -> bool:
    return (
        row.candidate_job_id == job_id
        and row.candidate_result_sha256 == command.candidate_result_sha256
        and row.wait_version == command.expected_wait_version
        and row.incident_version == command.expected_incident_version
        and row.idempotency_key == command.idempotency_key
        and row.command_sha256 == content_hash(command)
    )


async def validated_audit(session: AsyncSession, row: CandidatePlanCommandRow) -> PlanAuditRecord:
    record = PlanAuditRecord.model_validate(row, from_attributes=True)
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
        raise PlanError("candidate_plan_integrity_violation", "规划命令规范正文或摘要不一致")
    if row.outcome != "error":
        plan = await session.get(CandidatePlanRow, row.plan_id)
        if (
            plan is None
            or plan.investigation_run_id != row.investigation_run_id
            or plan.requested_by_id != row.subject_id
            or not matches_command(plan, row.candidate_job_id, record.command)
            or row.created_at < plan.created_at
            or row.outcome == "applied"
            and (plan.authorization_id != row.authorization_id or plan.created_at != row.created_at)
        ):
            raise PlanError("candidate_plan_integrity_violation", "规划审计缺少同源计划")
    return record


async def read_plan(
    session: AsyncSession, run: InvestigationRunRow, job_id: UUID, *, clock: Callable[[], datetime]
) -> PlanView:
    try:
        view, source, _ = await candidate_packet(session, run, job_id, clock=clock)
        row = await session.scalar(
            select(CandidatePlanRow).where(CandidatePlanRow.candidate_job_id == job_id)
        )
        if row is None:
            return PlanView(
                record=None, availability="pending" if view.availability != "stale" else "stale"
            )
        record = PlanRecord.model_validate(row, from_attributes=True)
        creator = await session.scalar(
            select(CandidatePlanCommandRow).where(
                CandidatePlanCommandRow.authorization_id == row.authorization_id
            )
        )
        if (
            view.result is None
            or view.status != "succeeded"
            or row.investigation_run_id != run.id
            or row.id != uuid5(job_id, "candidate-action-plan:v1")
            or row.policy_version != POLICY_VERSION
            or row.candidate_result_sha256 != view.result.content_sha256
            or row.wait_version != view.wait_version
            or row.created_at < view.result.created_at
            or record.content
            != build_plan(job_id, source, view.result.content, view.result.content_sha256)
            or row.content != record.content.model_dump(mode="json")
            or row.content_sha256 != content_hash(record.content)
            or creator is None
            or creator.outcome != "applied"
            or creator.plan_id != row.id
        ):
            raise PlanError("candidate_plan_integrity_violation", "计划内容或候选来源无法重建")
        await validated_audit(session, creator)
        incident = await session.get(IncidentRow, run.incident_id)
        stale = (
            view.availability == "stale"
            or incident is None
            or incident.version != row.incident_version
        )
        try:
            remaining_time(source.call_budget, source.started_at, normalized_now(clock))
        except CallBudgetExceeded:
            stale = True
        return PlanView(record=record, availability="stale" if stale else "current")
    except PlanError:
        raise
    except ValueError as error:
        raise PlanError(
            "candidate_plan_integrity_violation", "候选计划历史材料无法通过核验"
        ) from error
