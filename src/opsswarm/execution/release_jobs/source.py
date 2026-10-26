"""执行任务对124完整来源的重建与每次写前当前性校验。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.conditions.sources import source_availability
from opsswarm.execution.release.contracts import ReleaseRequestRecord
from opsswarm.execution.release.policy import validate_release_plan
from opsswarm.execution.release.queries import ReleaseContext, load_context, read_record
from opsswarm.execution.release.tables import CandidateReleaseRequestRow
from opsswarm.execution.release_jobs.contracts import ExecutionJobError
from opsswarm.investigation.persistence.tables import InvestigationRunRow


async def load_request(
    session: AsyncSession,
    run: InvestigationRunRow,
    candidate_job_id: UUID,
    version: int,
    *,
    clock: Callable[[], datetime],
) -> tuple[ReleaseContext, ReleaseRequestRecord | None]:
    context = await load_context(session, run, candidate_job_id, version, clock=clock)
    row = await session.scalar(
        select(CandidateReleaseRequestRow).where(
            CandidateReleaseRequestRow.approval_decision_id == context.approval.id
        )
    )
    return context, None if row is None else await read_record(session, row, context)


async def require_current(
    session: AsyncSession,
    run: InvestigationRunRow,
    context: ReleaseContext,
    request: ReleaseRequestRecord,
    *,
    clock: Callable[[], datetime],
) -> None:
    availability = await source_availability(session, run, context.basis, clock=clock)
    if availability != "current" or not request.created_at <= clock() < request.expires_at:
        raise ExecutionJobError(
            "release_execution_source_stale", "原审批、输入、预算或准备期限已失效"
        )
    validate_release_plan(context.basis.original.plan)
