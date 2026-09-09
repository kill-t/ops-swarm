"""父子任务共同事务中的收尾与成功前核对。"""

from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.investigation.models import EvidenceKind, InvestigationArtifacts
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.artifact_tables import AgentTaskResultRow
from opsswarm.investigation.tasks.contracts import (
    RoundStatus,
    TaskAssignment,
    TaskOutcome,
    TaskStatus,
)
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.events import append_task_event
from opsswarm.investigation.tasks.evidence_store import require_result_evidence
from opsswarm.investigation.tasks.policy import round_status_for_tasks
from opsswarm.investigation.tasks.results import SpecialistTaskResult, TaskResultRecord
from opsswarm.investigation.tasks.tables import (
    AgentTaskAttemptRow,
    AgentTaskRow,
    InvestigationRoundRow,
)
from opsswarm.investigation.working_summary.persistence import validated_summary
from opsswarm.investigation.working_summary.tables import AgentTaskWorkingSummaryRow


async def settle_round(session: AsyncSession, round_id: UUID, now: datetime) -> None:
    statuses = list(
        await session.scalars(select(AgentTaskRow.status).where(AgentTaskRow.round_id == round_id))
    )
    status = round_status_for_tasks([TaskStatus(value) for value in statuses])
    if status is RoundStatus.RUNNING:
        return
    row = await session.get(InvestigationRoundRow, round_id)
    if row is None:
        raise RuntimeError("任务关联轮次缺失")
    if row.status != "running":
        return
    row.status = status.value
    row.completed_at = now
    await append_task_event(
        session,
        row.investigation_run_id,
        occurred_at=now,
        event_type="round.completed" if status is RoundStatus.COMPLETED else "round.failed",
    )


async def interrupt_tasks(
    session: AsyncSession,
    run_id: UUID,
    *,
    now: datetime,
    will_retry: bool,
    expired: bool,
) -> None:
    """父 work_items 锁内调用，保留已成功结果并收敛所有未完成子任务。"""

    tasks = list(
        await session.scalars(
            select(AgentTaskRow)
            .where(
                AgentTaskRow.investigation_run_id == run_id,
                AgentTaskRow.status.in_(("pending", "running")),
            )
            .order_by(AgentTaskRow.id)
            .with_for_update()
        )
    )
    for task in tasks:
        attempt = await session.scalar(
            select(AgentTaskAttemptRow).where(
                AgentTaskAttemptRow.task_id == task.id,
                AgentTaskAttemptRow.status == "running",
            )
        )
        if attempt is not None:
            attempt.status = "lease_expired" if expired else "failed"
            attempt.finished_at = now
            attempt.outcome = (
                None
                if expired
                else TaskOutcome(
                    status="failed",
                    error_code="parent_attempt_interrupted",
                    retryable=will_retry,
                ).model_dump(mode="json")
            )
        next_status = (
            "pending" if will_retry and task.attempt_count < task.max_attempts else "failed"
        )
        if attempt is None and task.status == next_status:
            continue
        task.status = next_status
        task.version += 1
        task.updated_at = now
        task.completed_at = now if next_status == "failed" else None
        await append_task_event(
            session, run_id, task_id=task.id, occurred_at=now, event_type="task.interrupted"
        )
    for round_id in {task.round_id for task in tasks}:
        await settle_round(session, round_id, now)


async def require_complete_results(
    session: AsyncSession,
    run_id: UUID,
    artifacts: InvestigationArtifacts,
) -> None:
    """v2 父运行不得仅凭 checkpoint 声称专业工作已经完成。"""

    rows = (
        await session.execute(
            select(AgentTaskRow, AgentTaskResultRow)
            .outerjoin(AgentTaskResultRow, AgentTaskResultRow.task_id == AgentTaskRow.id)
            .where(AgentTaskRow.investigation_run_id == run_id)
        )
    ).all()
    if len(rows) != 3 or any(task.status != "succeeded" or result is None for task, result in rows):
        raise TaskFactConflict("task_results_incomplete", "v2 完成前必须提交三个专业结果")
    round_ids = {task.round_id for task, _ in rows}
    round_row = await session.get(InvestigationRoundRow, next(iter(round_ids)))
    if (
        len(round_ids) != 1
        or round_row is None
        or round_row.round_number != 1
        or round_row.status != "completed"
    ):
        raise TaskFactConflict("task_round_mismatch", "v2 固定图必须完成唯一首轮")
    results = [SpecialistTaskResult.model_validate(result.result) for _, result in rows]
    for (task, _), result in zip(rows, results, strict=True):
        await require_result_evidence(session, run_id, task, result)
    run = await session.get(InvestigationRunRow, run_id)
    assert run is not None
    if run.working_summary_policy_version is not None:
        for task, result_row in rows:
            summary = await session.get(AgentTaskWorkingSummaryRow, task.id)
            if summary is None:
                raise TaskFactConflict("working_summary_missing", "运行完成前必须有三份工作摘要")
            await validated_summary(
                session,
                task,
                TaskResultRecord.model_validate(result_row, from_attributes=True),
                summary,
            )
    assignments = [TaskAssignment.model_validate(task.assignment) for task, _ in rows]
    if (
        {item.report.specialist for item in results} != set(EvidenceKind)
        or len({item.evidence_snapshot_id for item in results}) != 1
        or any(
            result.report.specialist is not assignment.role
            or result.evidence_snapshot_id != assignment.evidence_snapshot_id
            for result, assignment in zip(results, assignments, strict=True)
        )
        or sorted((item.report for item in results), key=lambda r: r.specialist.value)
        != sorted(artifacts.specialist_reports, key=lambda r: r.specialist.value)
        or sorted((e for item in results for e in item.evidence), key=lambda e: e.evidence_id)
        != sorted(artifacts.evidence_items, key=lambda e: e.evidence_id)
        or artifacts.result.tool_call_count != 3
    ):
        raise TaskFactConflict("task_results_mismatch", "图结果与已提交专业快照不一致")
