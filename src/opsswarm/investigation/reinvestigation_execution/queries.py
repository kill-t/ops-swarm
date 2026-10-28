"""按原任务读取独立执行投影，并核验已保存专业材料和结果。"""

from collections.abc import Callable
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.execution.release.observer import utc_now
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_dispatch.tables import ReflowDispatchTaskRow
from opsswarm.investigation.reinvestigation_execution.contracts import (
    ExecutionAttempt,
    ExecutionResult,
    ExecutionTaskView,
    ExecutionView,
)
from opsswarm.investigation.reinvestigation_execution.source import load_source, validate_work
from opsswarm.investigation.reinvestigation_execution.tables import (
    ExecutionAttemptRow,
    ExecutionEvidenceRow,
    ExecutionPacketRow,
    ExecutionResultRow,
    ExecutionWorkRow,
)
from opsswarm.investigation.reinvestigation_execution.validation import (
    validated_evidence,
    validated_model_call,
    validated_packet,
)


class ExecutionQueries:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ):
        self._factory, self._clock = factory, clock

    async def get(self, run_id: UUID) -> ExecutionView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            await session.scalar(
                select(IncidentRow)
                .where(IncidentRow.id == run.incident_id)
                .with_for_update(read=True)
            )
            tasks = list(
                await session.scalars(
                    select(ReflowDispatchTaskRow)
                    .where(ReflowDispatchTaskRow.investigation_run_id == run.id)
                    .order_by(ReflowDispatchTaskRow.task_key)
                )
            )
            views = []
            for task in tasks:
                source = await load_source(session, run, task.id, clock=self._clock)
                work = await session.get(ExecutionWorkRow, task.id)
                if work is None:
                    views.append(
                        ExecutionTaskView(
                            task_id=task.id,
                            status="pending",
                            effective_window=None,
                            error_code=None,
                            attempts=(),
                            evidence=None,
                            packet=None,
                            result=None,
                        )
                    )
                    continue
                window = validate_work(work, source)
                attempts = list(
                    await session.scalars(
                        select(ExecutionAttemptRow)
                        .where(ExecutionAttemptRow.task_id == task.id)
                        .order_by(ExecutionAttemptRow.number)
                    )
                )
                if len(attempts) != work.attempt_count or any(
                    a.number != i for i, a in enumerate(attempts, 1)
                ):
                    raise ReflowError("execution_integrity_violation", "专业尝试序列不连续")
                evidence = await validated_evidence(session, work, source)
                packet = await validated_packet(session, work, source)
                row = await session.get(ExecutionResultRow, task.id)
                result = None
                if row is not None:
                    result = ExecutionResult.model_validate(row.content)
                    model = await validated_model_call(session, work, result.model_call_id, source)
                    saved_evidence = await session.get(ExecutionEvidenceRow, task.id)
                    saved_packet = await session.get(ExecutionPacketRow, task.id)
                    if (
                        saved_evidence is None
                        or saved_packet is None
                        or work.status != "succeeded"
                        or result.task_id != task.id
                        or result.investigation_run_id != run.id
                        or result.dispatch_sha256 != work.dispatch_sha256
                        or result.evidence_call_id != saved_evidence.call_id
                        or result.evidence_sha256 != saved_evidence.snapshot_sha256
                        or result.packet_sha256 != saved_packet.packet_sha256
                        or result.report != model.report
                        or row.created_at != result.created_at
                        or work.completed_at != row.created_at
                        or not attempts
                        or row.attempt_id != attempts[-1].id
                        or attempts[-1].status != "succeeded"
                        or attempts[-1].finished_at != result.created_at
                        or row.content_sha256 != content_hash(result)
                        or row.content_sha256 != raw_content_hash(row.content)
                    ):
                        raise ReflowError(
                            "execution_integrity_violation", "专业结果缺少完整同源终态闭环"
                        )
                elif work.status == "succeeded":
                    raise ReflowError("execution_integrity_violation", "成功专业任务缺少结果")
                views.append(
                    ExecutionTaskView(
                        task_id=task.id,
                        status=cast(
                            Literal["queued", "running", "succeeded", "blocked", "failed"],
                            work.status,
                        ),
                        effective_window=window,
                        error_code=work.last_error,
                        attempts=tuple(
                            ExecutionAttempt.model_validate(a, from_attributes=True)
                            for a in attempts
                        ),
                        evidence=evidence,
                        packet=packet,
                        result=result,
                    )
                )
            return ExecutionView(investigation_run_id=run.id, tasks=tuple(views))
