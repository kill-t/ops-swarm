"""原批准、完整核验前缀及当前预算的单次来源重建。"""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.conditions.contracts import ApprovalAvailability, ApprovalError
from opsswarm.execution.candidate.queries import PreflightContext
from opsswarm.execution.conditions.contracts import ConditionRecord
from opsswarm.execution.conditions.policy import verification_availability
from opsswarm.execution.conditions.queries import read_chain
from opsswarm.execution.conditions.sources import ConditionSources, load_context
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.planning.candidate.budget import check_planning_budget
from opsswarm.planning.candidate.contracts import PlanError


@dataclass(frozen=True)
class ApprovalBasis:
    original: PreflightContext
    verification: ConditionRecord
    latest_version: int


async def load_basis(
    session: AsyncSession,
    run: InvestigationRunRow,
    job_id: UUID,
    version: int,
    *,
    clock: Callable[[], datetime],
) -> ApprovalBasis:
    try:
        context = await load_context(session, run, job_id, clock=clock)
        chain = await read_chain(session, ConditionSources(session, context))
        if isinstance(version, bool) or not 1 <= version <= len(chain):
            raise ApprovalError("condition_approval_verification_not_found", "核验版本不存在", 404)
        return ApprovalBasis(context, chain[version - 1], chain[-1].version)
    except ApprovalError:
        raise
    except (ValueError, CallBudgetExceeded, TaskFactConflict) as error:
        raise ApprovalError(
            "condition_approval_source_invalid",
            "核验审批来源无法完整重建",
            404 if getattr(error, "status_code", None) == 404 else 409,
        ) from error


async def source_availability(
    session: AsyncSession,
    run: InvestigationRunRow,
    basis: ApprovalBasis,
    *,
    clock: Callable[[], datetime],
) -> ApprovalAvailability:
    try:
        budget = await read_budget_snapshot(session, run)
        budget_current = not any(call.status == "reserved" for call in budget.calls)
        if budget_current:
            try:
                check_planning_budget(
                    basis.original.source, basis.original.packet, budget, now=normalized_now(clock)
                )
            except (PlanError, CallBudgetExceeded):
                budget_current = False
        return verification_availability(
            basis.verification,
            basis.latest_version,
            basis.original.decision.availability,
            normalized_now(clock),
            budget_current=budget_current,
        )
    except (ValueError, TaskFactConflict) as error:
        raise ApprovalError("condition_approval_source_invalid", "原预算无法完整重建") from error
