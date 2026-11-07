"""恢复验证绑定完整执行历史，不把旧批准转换为当前写权限。"""

from collections.abc import Callable
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.execution.release.contracts import ReleaseRequestRecord
from opsswarm.execution.release.tables import CandidateReleaseRequestRow
from opsswarm.execution.release_jobs.contracts import ExecutionJobView
from opsswarm.execution.release_jobs.queries import read_view
from opsswarm.execution.release_jobs.tables import ReleaseExecutionJobRow
from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.verification.candidate.contracts import RecoveryError


async def execution_source(
    session: AsyncSession,
    run: InvestigationRunRow,
    execution_id: UUID,
    *,
    clock: Callable[[], datetime],
) -> ExecutionJobView:
    row = await session.get(ReleaseExecutionJobRow, execution_id)
    if row is None or row.investigation_run_id != run.id:
        raise RecoveryError("recovery_source_missing", "恢复验证缺少原执行任务", 404)
    # 恢复只读取冻结历史。原生守卫核验完整批准链，当前写权限仍由执行入口校验。
    try:
        await session.execute(
            text(
                "SELECT assert_release_job_history(job) "
                "FROM candidate_release_execution_jobs job WHERE id=:id"
            ),
            {"id": row.id},
        )
    except DBAPIError as error:
        if getattr(error.orig, "sqlstate", None) != "P0001":
            raise
        raise RecoveryError("recovery_integrity_violation", "原执行历史无法完整校验") from error
    request_row = await session.get(CandidateReleaseRequestRow, row.request_id)
    if request_row is None:
        raise RecoveryError("recovery_integrity_violation", "恢复验证缺少原准备事实")
    request = ReleaseRequestRecord.model_validate(request_row, from_attributes=True)
    view = await read_view(session, row, request)
    return view


def fixed_window(run: InvestigationRunRow, source: ExecutionJobView) -> tuple[datetime, datetime]:
    assert source.record is not None and source.record.completed_at is not None
    if run.call_budget is None or run.started_at is None:
        raise RecoveryError("recovery_integrity_violation", "原运行缺少固定预算与开始时间")
    policy = CallBudgetPolicy.model_validate(run.call_budget)
    start = source.record.completed_at
    return start, min(
        start + timedelta(seconds=120),
        run.started_at + timedelta(seconds=policy.max_duration_seconds),
    )
