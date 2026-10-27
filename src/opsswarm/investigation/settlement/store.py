"""在父锁与单次事务内收尾，不引入第二套租约或网络调用。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.execution.release.observer import utc_now
from opsswarm.execution.release.tables import CandidateReleaseRequestRow
from opsswarm.execution.release_jobs.store import lock_parent
from opsswarm.investigation.candidate.contracts import CandidateSource
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow
from opsswarm.investigation.settlement.contracts import (
    EVENT_TYPE,
    SettlementContent,
    SettlementError,
    SettlementRecord,
    SettlementView,
)
from opsswarm.investigation.settlement.eligibility import eligible_recoveries
from opsswarm.investigation.settlement.parent_state import parent_settlement
from opsswarm.investigation.settlement.policy import disposition, settlement_id
from opsswarm.investigation.settlement.tables import CandidateSettlementRow
from opsswarm.investigation.tasks.artifact_tables import AgentTaskEventRow
from opsswarm.investigation.tasks.events import append_task_event
from opsswarm.verification.candidate.contracts import RecoveryView
from opsswarm.verification.candidate.queries import recovery_view
from opsswarm.verification.candidate.source import execution_source
from opsswarm.verification.candidate.tables import RecoveryJobRow


class CandidateSettlementStore:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._factory, self._clock = factory, clock

    async def get(self, run_id: UUID) -> SettlementView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            row = await session.scalar(
                select(CandidateSettlementRow).where(
                    CandidateSettlementRow.investigation_run_id == run_id
                )
            )
            if row is None:
                if run.status == "settled":
                    raise SettlementError(
                        "candidate_settlement_integrity_violation", "父终态缺少收尾事实"
                    )
                return SettlementView(record=None)
            return SettlementView(record=await self._read(session, run))

    async def _read(self, session: AsyncSession, run: InvestigationRunRow) -> SettlementRecord:
        record = await parent_settlement(session, run)
        row = await session.get(RecoveryJobRow, record.recovery_job_id)
        assert row is not None
        source = await execution_source(session, run, row.id, clock=self._clock)
        await recovery_view(session, row, run, source)
        assert source.result is not None
        request = await session.get(CandidateReleaseRequestRow, source.result.request_id)
        if request is None or request.incident_version != record.incident_version:
            raise SettlementError(
                "candidate_settlement_integrity_violation", "收尾与原事故版本不一致"
            )
        return record

    async def settle_next(self) -> bool:
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                recovery = await session.scalar(
                    eligible_recoveries()
                    .outerjoin(
                        CandidateSettlementRow,
                        CandidateSettlementRow.investigation_run_id
                        == RecoveryJobRow.investigation_run_id,
                    )
                    .where(
                        RecoveryJobRow.id.not_in(skipped),
                        RecoveryJobRow.status == "completed",
                        CandidateSettlementRow.id.is_(None),
                        InvestigationRunRow.status.in_(("await_candidate", "await_human")),
                    )
                    .order_by(RecoveryJobRow.completed_at, RecoveryJobRow.id)
                    .limit(1)
                )
                if recovery is None:
                    return False
                skipped.append(recovery.id)
                work = await session.scalar(
                    select(WorkItemRow)
                    .where(WorkItemRow.investigation_run_id == recovery.investigation_run_id)
                    .with_for_update(skip_locked=True)
                )
                if work is None:
                    continue
                run = await lock_parent(
                    session, recovery.investigation_run_id, recovery.candidate_job_id
                )
                existing = await session.scalar(
                    select(CandidateSettlementRow).where(
                        CandidateSettlementRow.investigation_run_id == run.id
                    )
                )
                if existing is not None:
                    await self._read(session, run)
                    continue
                source = await execution_source(session, run, recovery.id, clock=self._clock)
                view = await recovery_view(session, recovery, run, source)
                if not await _eligible(session, run, recovery, view):
                    continue
                assert source.result is not None and view.result is not None
                job = await session.get(CandidateJobRow, recovery.candidate_job_id)
                assert job is not None
                original = CandidateSource.model_validate(job.source)
                request = await session.get(CandidateReleaseRequestRow, source.result.request_id)
                assert request is not None
                cursor = (
                    int(
                        await session.scalar(
                            select(func.coalesce(func.max(AgentTaskEventRow.cursor), 0)).where(
                                AgentTaskEventRow.investigation_run_id == run.id
                            )
                        )
                        or 0
                    )
                    + 1
                )
                now = normalized_now(self._clock)
                content = SettlementContent(
                    id=settlement_id(run.id),
                    investigation_run_id=run.id,
                    recovery_job_id=recovery.id,
                    candidate_job_id=job.id,
                    verification_version=recovery.verification_version,
                    operation_id=recovery.operation_id,
                    execution_result_sha256=recovery.execution_result_sha256,
                    recovery_result_sha256=content_hash(view.result),
                    handoff_id=original.handoff.id,
                    previous_run_status="await_human"
                    if run.status == "await_human"
                    else "await_candidate",
                    previous_run_updated_at=run.updated_at,
                    incident_version=request.incident_version,
                    outcome=view.result.outcome,
                    disposition=disposition(view.result),
                    reason_code=view.result.reason_code,
                    event_cursor=cursor,
                    created_at=now,
                )
                session.add(
                    CandidateSettlementRow(
                        **content.model_dump(), content_sha256=content_hash(content)
                    )
                )
                await session.flush()
                await append_task_event(session, run.id, event_type=EVENT_TYPE, occurred_at=now)
                await session.flush()
                run.status, run.completed_at, run.updated_at = "settled", now, now
                await session.flush()
                await self._read(session, run)
                return True
        return False


async def _eligible(
    session: AsyncSession,
    run: InvestigationRunRow,
    row: RecoveryJobRow,
    view: RecoveryView,
) -> bool:
    if view.result is None:
        return False
    return (
        await session.scalar(
            eligible_recoveries().where(
                RecoveryJobRow.id == row.id,
                RecoveryJobRow.investigation_run_id == run.id,
            )
        )
        is not None
    )
