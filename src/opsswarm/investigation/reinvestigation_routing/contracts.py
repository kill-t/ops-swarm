"""精确来源、历史预算及当前适用性分开表达。"""

from typing import Literal
from uuid import UUID

from pydantic import Field

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.human_routing.contracts import UnresolvedConstraint
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel
from opsswarm.investigation.reinvestigation_execution.contracts import EffectiveWindow
from opsswarm.investigation.review_routing.contracts import ReviewRoute, ReviewRoutingDecision
from opsswarm.verification.candidate.contracts import UtcTime

ROUTE = "/api/v1/investigations/{run_id}/reinvestigation-routing"
BasisBlocker = Literal["reflow_source_changed", "run_deadline_exceeded"]
Blocker = Literal[
    "reflow_source_changed",
    "run_deadline_exceeded",
    "human_constraints_unresolved",
    "model_call_budget_exceeded",
    "tool_call_budget_exceeded",
    "run_input_budget_exceeded",
    "run_output_budget_exceeded",
    "model_cost_budget_exceeded",
    "model_usage_exceeded_allocation",
]


class RoutingWindow(HumanWaitModel):
    task_id: UUID
    effective_window: EffectiveWindow


class ReflowRoutingContent(HumanWaitModel):
    version: Literal["reinvestigation-routing-v1"] = "reinvestigation-routing-v1"
    status: Literal["recorded"] = "recorded"
    dispatch_status: Literal["not_dispatched"] = "not_dispatched"
    investigation_run_id: UUID
    review_source_sha256: Hash
    review_packet_sha256: Hash
    review_result_sha256: Hash
    synthesis_result_sha256: Hash
    source_call_id: UUID
    basis_event_cursor: int = Field(ge=1, strict=True)
    basis_blocker: BasisBlocker | None
    base_decision: ReviewRoutingDecision
    route: ReviewRoute
    blocking_reason_codes: tuple[Blocker, ...] = Field(max_length=9)
    unresolved_constraints: tuple[UnresolvedConstraint, ...] = Field(max_length=75)
    unresolved_questions: tuple[str, ...] = Field(min_length=1, max_length=20)
    observation_windows: tuple[RoutingWindow, ...] = Field(min_length=1, max_length=3)
    minimum_model_calls: int = Field(ge=0, le=5, strict=True)
    minimum_tool_calls: int = Field(ge=0, le=3, strict=True)
    budget: CallBudgetSnapshot
    created_at: UtcTime


class ReflowRoutingView(HumanWaitModel):
    record: ReflowRoutingContent | None
    availability: Literal["pending", "current", "stale", "expired", "blocked"]
    blocking_reason_codes: tuple[str, ...]
