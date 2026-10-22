"""历史路由事实与当前适用性分开表达。"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.human_wait.contracts import Hash, HumanWaitModel
from opsswarm.investigation.review_routing.contracts import ReviewRoute, ReviewRoutingDecision

Blocker = Literal[
    "human_constraints_unresolved",
    "run_deadline_exceeded",
    "model_call_budget_exceeded",
    "tool_call_budget_exceeded",
    "run_input_budget_exceeded",
    "run_output_budget_exceeded",
    "model_cost_budget_exceeded",
    "model_usage_exceeded_allocation",
]


class UnresolvedConstraint(HumanWaitModel):
    event_id: UUID
    item_index: int = Field(ge=0, le=4, strict=True)
    text: str = Field(min_length=1, max_length=1000)


class HumanRoutingDecision(HumanWaitModel):
    version: Literal["human-review-routing-v1"] = "human-review-routing-v1"
    dispatch_status: Literal["not_dispatched"] = "not_dispatched"
    base_decision: ReviewRoutingDecision
    route: ReviewRoute
    blocking_reason_codes: tuple[Blocker, ...] = Field(max_length=8)
    unresolved_constraints: tuple[UnresolvedConstraint, ...] = Field(max_length=75)
    minimum_model_calls: int = Field(ge=0, le=5, strict=True)
    minimum_tool_calls: int = Field(ge=0, le=3, strict=True)
    budget: CallBudgetSnapshot

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        if len(set(self.blocking_reason_codes)) != len(self.blocking_reason_codes):
            raise ValueError("路由阻断原因不能重复")
        if self.route != (
            "await_human" if self.blocking_reason_codes else self.base_decision.route
        ):
            raise ValueError("最终意图必须服从确定性阻断")
        if bool(self.unresolved_constraints) != (
            "human_constraints_unresolved" in self.blocking_reason_codes
        ):
            raise ValueError("人工约束与阻断原因不一致")
        return self


class HumanRoutingRecord(HumanWaitModel):
    id: UUID
    job_id: UUID
    investigation_run_id: UUID
    wait_version: int = Field(ge=2, le=16, strict=True)
    preparation_source_sha256: Hash
    result_sha256: Hash
    source_call_id: UUID
    decision: HumanRoutingDecision
    content_sha256: Hash
    created_at: AwareDatetime


class HumanRoutingView(HumanWaitModel):
    record: HumanRoutingRecord | None
    availability: Literal["pending", "current", "stale"]
    unavailable_reason: str | None
    current_blocking_reason_codes: tuple[Blocker, ...] = Field(max_length=8)


class HumanRoutingError(ValueError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        super().__init__(message)
        self.code, self.status_code = code, status_code
