"""按完整来源及原账本前缀重建历史，当前变化单独诊断。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.execution.release.observer import utc_now
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.reinvestigation.queries import historical_budget
from opsswarm.investigation.reinvestigation_review.contracts import ReflowReviewError
from opsswarm.investigation.reinvestigation_routing.contracts import (
    ReflowRoutingContent,
    ReflowRoutingView,
)
from opsswarm.investigation.reinvestigation_routing.policy import compile_route
from opsswarm.investigation.reinvestigation_routing.source import (
    current_basis,
    historical_basis,
    load_source,
)
from opsswarm.investigation.reinvestigation_routing.tables import ReflowRouteRow


class ReflowRoutingQueries:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ):
        self._factory, self._clock = factory, clock

    async def get(self, run_id: UUID) -> ReflowRoutingView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            await session.scalar(
                select(IncidentRow)
                .where(IncidentRow.id == run.incident_id)
                .with_for_update(read=True)
            )
            row = await session.get(ReflowRouteRow, run_id)
            if row is None:
                return ReflowRoutingView(
                    record=None, availability="pending", blocking_reason_codes=()
                )
            try:
                source, review = await load_source(session, run, clock=self._clock)
                content = ReflowRoutingContent.model_validate(row.content)
                budget = await read_budget_snapshot(session, run)
                captured = historical_budget(budget, content.budget, content.created_at)
                expected = compile_route(
                    source,
                    review,
                    captured,
                    now=content.created_at,
                    basis_event_cursor=content.basis_event_cursor,
                    basis_blocker=await historical_basis(
                        session, run, content.basis_event_cursor, content.created_at
                    ),
                )
                if (
                    content != expected
                    or row.investigation_run_id != run.id
                    or row.route != content.route
                    or row.created_at != content.created_at
                    or row.content_sha256 != content_hash(content)
                    or row.content_sha256 != raw_content_hash(row.content)
                ):
                    raise ValueError("路由正文或权威来源不一致")
                now = normalized_now(self._clock)
                reason = await current_basis(session, run, now)
                if any(c.status == "reserved" for c in budget.calls):
                    blockers: tuple[str, ...] = (reason,) if reason else ("calls_in_flight",)
                else:
                    blockers = compile_route(
                        source,
                        review,
                        budget,
                        now=now,
                        basis_event_cursor=content.basis_event_cursor,
                        basis_blocker=reason,
                    ).blocking_reason_codes
                return ReflowRoutingView(
                    record=content,
                    availability="stale"
                    if reason == "reflow_source_changed"
                    else "expired"
                    if reason == "run_deadline_exceeded"
                    else "blocked"
                    if blockers
                    else "current",
                    blocking_reason_codes=blockers,
                )
            except (ValueError, TypeError) as error:
                raise ReflowReviewError(
                    "reflow_routing_integrity_violation", "回流路由历史无法从完整来源重建"
                ) from error
