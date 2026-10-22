"""重建历史路由及原账本前缀；当前输入和阻断诊断独立返回。"""

from collections.abc import Callable
from datetime import datetime
from uuid import uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.model_policy import summarize_model_usage
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_review.queries import read_job_packet
from opsswarm.investigation.human_review.tables import HumanReviewJobRow
from opsswarm.investigation.human_routing.contracts import (
    Blocker,
    HumanRoutingError,
    HumanRoutingRecord,
    HumanRoutingView,
)
from opsswarm.investigation.human_routing.policy import compile_human_routing
from opsswarm.investigation.human_routing.tables import HumanReviewRouteRow
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow


async def read_route(
    session: AsyncSession,
    job: HumanReviewJobRow,
    run: InvestigationRunRow,
    *,
    clock: Callable[[], datetime],
) -> HumanRoutingView:
    view, source = await read_job_packet(session, job, run, clock=clock)
    budget = await read_budget_snapshot(session, run)
    row = await session.scalar(
        select(HumanReviewRouteRow).where(HumanReviewRouteRow.job_id == job.id)
    )
    record = None
    if row is not None:
        try:
            record = HumanRoutingRecord.model_validate(row, from_attributes=True)
            captured = record.decision.budget
            end = captured.calls[-1].sequence if captured.calls else 0
            prefix = tuple(call for call in budget.calls if call.sequence <= end)
            rebuilt_budget = budget.model_copy(
                update={
                    "calls": prefix,
                    "reserved_model_calls": sum(c.kind == "model" for c in prefix),
                    "reserved_tool_calls": sum(c.kind == "tool" for c in prefix),
                    "model_usage": summarize_model_usage(
                        source.model_budget,
                        [
                            (c.model_allocation, c.model_usage)
                            for c in prefix
                            if c.kind == "model" and c.model_allocation is not None
                        ],
                    ),
                }
            )
            if (
                view.result is None
                or record.id != uuid5(job.id, "human-review-routing:v1")
                or record.investigation_run_id != run.id
                or record.wait_version != job.wait_version
                or record.preparation_source_sha256 != content_hash(source)
                or record.result_sha256 != view.result.content_sha256
                or record.source_call_id != view.result.source_call_id
                or record.created_at < view.result.created_at
                or captured != rebuilt_budget
                or record.source_call_id not in {call.id for call in prefix}
                or any(call.reserved_at > record.created_at for call in prefix)
                or any(
                    call.sequence > end and call.reserved_at < record.created_at
                    for call in budget.calls
                )
                or record.decision
                != compile_human_routing(
                    source, view.result.content, captured, now=record.created_at
                )
                or row.decision != record.decision.model_dump(mode="json")
                or record.content_sha256 != content_hash(record.decision)
            ):
                raise ValueError("路由内容或成功来源不一致")
        except ValueError as error:
            raise HumanRoutingError(
                "human_routing_integrity_violation", "重评路由来源无法通过核验"
            ) from error
    incident = await session.get(IncidentRow, run.incident_id)
    stale = (
        view.availability == "stale"
        or run.status != "await_human"
        or incident is None
        or incident.status != "investigating"
    )
    in_flight = any(call.status == "reserved" for call in budget.calls)
    blockers: tuple[Blocker, ...] = ()
    if view.result and not in_flight:
        blockers = compile_human_routing(
            source, view.result.content, budget, now=normalized_now(clock)
        ).blocking_reason_codes
    return HumanRoutingView(
        record=record,
        availability="stale" if stale else "current" if record else "pending",
        unavailable_reason="human_routing_source_stale"
        if stale
        else "human_routing_calls_in_flight"
        if in_flight
        else "human_routing_not_recorded"
        if record is None
        else None,
        current_blocking_reason_codes=blockers,
    )
