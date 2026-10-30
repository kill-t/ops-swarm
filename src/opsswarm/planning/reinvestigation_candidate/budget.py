"""纯规划不新预留，但仍检查所有原调用、未知占用和总期限。"""

from datetime import datetime

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.reinvestigation_candidate.contracts import (
    ReflowCandidateError,
    ReflowCandidatePacket,
    ReflowCandidateSource,
)
from opsswarm.investigation.reinvestigation_candidate.policy import check_budget
from opsswarm.planning.reinvestigation_candidate.contracts import ReflowPlanError


def check_planning_budget(
    source: ReflowCandidateSource,
    packet: ReflowCandidatePacket,
    budget: CallBudgetSnapshot,
    *,
    now: datetime,
) -> None:
    try:
        check_budget(source, packet, budget, now=now, allocate=False)
    except (CallBudgetExceeded, ReflowCandidateError) as error:
        raise ReflowPlanError(error.code, "原累计预算或截止时间不允许生成回流草案") from error
