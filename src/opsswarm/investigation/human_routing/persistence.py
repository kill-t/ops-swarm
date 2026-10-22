"""在原 work 锁内保存成功重评的后续意图，不申请新调用或重开旧任务。"""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_review.queries import read_job_packet
from opsswarm.investigation.human_review.tables import HumanReviewJobRow
from opsswarm.investigation.human_routing.policy import compile_human_routing
from opsswarm.investigation.human_routing.tables import HumanReviewRouteRow
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow


async def record_route(
    session: AsyncSession,
    job: HumanReviewJobRow,
    run: InvestigationRunRow,
    *,
    clock: Callable[[], datetime],
) -> bool:
    if job.status != "succeeded" or await session.scalar(
        select(HumanReviewRouteRow.id).where(HumanReviewRouteRow.job_id == job.id)
    ):
        return False
    incident = await session.scalar(
        select(IncidentRow).where(IncidentRow.id == run.incident_id).with_for_update()
    )
    if run.status != "await_human" or incident is None or incident.status != "investigating":
        return False
    view, source = await read_job_packet(session, job, run, clock=clock)
    if view.availability == "stale":
        return False
    assert view.result is not None
    budget = await read_budget_snapshot(session, run)
    if any(call.status == "reserved" for call in budget.calls):
        return False
    now = normalized_now(clock)
    decision = compile_human_routing(source, view.result.content, budget, now=now)
    session.add(
        HumanReviewRouteRow(
            id=uuid5(job.id, "human-review-routing:v1"),
            job_id=job.id,
            investigation_run_id=run.id,
            wait_version=job.wait_version,
            preparation_source_sha256=content_hash(source),
            result_sha256=view.result.content_sha256,
            source_call_id=view.result.source_call_id,
            decision=decision.model_dump(mode="json"),
            content_sha256=content_hash(decision),
            created_at=now,
        )
    )
    await session.flush()
    return True
