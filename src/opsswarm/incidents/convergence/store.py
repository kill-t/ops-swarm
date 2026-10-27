"""以父锁和事故锁组织唯一收敛事实、合法迁移与槽位投影。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.execution.release.observer import utc_now
from opsswarm.execution.release_jobs.store import lock_parent
from opsswarm.incidents.convergence.contracts import ConvergenceError, ConvergenceView
from opsswarm.incidents.convergence.policy import compile_convergence
from opsswarm.incidents.convergence.queries import read_convergence
from opsswarm.incidents.convergence.tables import CandidateIncidentConvergenceRow
from opsswarm.incidents.models import ActorType, IncidentActor, IncidentStatus
from opsswarm.incidents.persistence.repository import transition_incident_in_session
from opsswarm.incidents.persistence.tables import AlertRow, IncidentRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import WorkItemRow
from opsswarm.investigation.settlement.queries import read_settlement_sources
from opsswarm.investigation.settlement.tables import CandidateSettlementRow


class CandidateIncidentConvergenceStore:
    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._factory, self._clock = factory, clock

    async def get(self, run_id: UUID) -> ConvergenceView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            row = await session.get(CandidateIncidentConvergenceRow, run_id)
            if row is None:
                if run.candidate_slot_released_at is not None:
                    raise ConvergenceError(
                        "candidate_convergence_integrity_violation", "槽位释放缺少收敛事实"
                    )
                return ConvergenceView(record=None)
            return ConvergenceView(
                record=await read_convergence(session, run, row, clock=self._clock)
            )

    async def converge_next(self) -> bool:
        skipped: list[UUID] = []
        async with self._factory() as session, session.begin():
            for _ in range(8):
                candidate = await session.scalar(
                    select(CandidateSettlementRow)
                    .outerjoin(
                        CandidateIncidentConvergenceRow,
                        CandidateIncidentConvergenceRow.investigation_run_id
                        == CandidateSettlementRow.investigation_run_id,
                    )
                    .where(
                        CandidateSettlementRow.investigation_run_id.not_in(skipped),
                        CandidateIncidentConvergenceRow.investigation_run_id.is_(None),
                    )
                    .order_by(CandidateSettlementRow.created_at, CandidateSettlementRow.id)
                    .limit(1)
                )
                if candidate is None:
                    return False
                run_id = candidate.investigation_run_id
                skipped.append(run_id)
                work = await session.scalar(
                    select(WorkItemRow)
                    .where(
                        WorkItemRow.investigation_run_id == run_id,
                    )
                    .with_for_update(skip_locked=True)
                )
                if work is None:
                    continue
                run = await lock_parent(session, run_id, candidate.candidate_job_id)
                prior = await session.get(CandidateIncidentConvergenceRow, run_id)
                if prior is not None:
                    await read_convergence(session, run, prior, clock=self._clock)
                    continue
                settlement, recovery = await read_settlement_sources(
                    session, run, clock=self._clock
                )
                incident = await session.get(IncidentRow, run.incident_id)
                assert incident is not None
                alert = await session.scalar(
                    select(AlertRow)
                    .where(
                        AlertRow.incident_id == incident.id,
                    )
                    .order_by(AlertRow.ingested_at.desc(), AlertRow.id.desc())
                    .limit(1)
                )
                now = normalized_now(self._clock)
                content = compile_convergence(
                    settlement,
                    recovery,
                    incident_id=incident.id,
                    status=IncidentStatus(incident.status),
                    version=incident.version,
                    latest_alert_id=alert.id if alert else None,
                    latest_alert_ingested_at=alert.ingested_at if alert else None,
                    now=now,
                )
                row = CandidateIncidentConvergenceRow(
                    investigation_run_id=run_id,
                    incident_id=incident.id,
                    content=content.model_dump(mode="json"),
                    content_sha256=content_hash(content),
                    created_at=now,
                )
                session.add(row)
                await session.flush()
                for number, action in enumerate(content.actions):
                    await transition_incident_in_session(
                        session,
                        incident.id,
                        action=action,
                        expected_version=content.before_version + number,
                        occurred_at=now,
                        actor=IncidentActor(ActorType.SYSTEM, f"candidate-convergence:{run_id}"),
                        reason=content.reason_code,
                    )
                if content.slot_released:
                    run.candidate_slot_released_at = now
                    await session.flush()
                await read_convergence(session, run, row, clock=self._clock)
                return True
        return False
