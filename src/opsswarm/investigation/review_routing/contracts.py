"""开放假设审查的确定性路由契约。"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.review.contracts import FollowUpSuggestion
from opsswarm.investigation.tasks.contracts import TaskModel

ReviewRoute = Literal["follow_up", "await_human", "candidate_evaluation"]
ReviewRouteReason = Literal[
    "support_sufficient",
    "support_insufficient",
    "hypothesis_counter_evidence_present",
    "review_counter_evidence_present",
    "evidence_gaps_present",
    "competing_explanations_present",
    "follow_up_suggestions_missing",
    "follow_up_suggestions_present",
    "round_limit_reached",
    "per_round_task_limit_exceeded",
    "total_task_limit_exceeded",
    "candidate_requirements_satisfied",
    "follow_up_capacity_available",
]
RoutingRuleVersion = Literal["deterministic-review-routing-v1"]
ROUTING_RULE_VERSION: RoutingRuleVersion = "deterministic-review-routing-v1"

_REASON_ORDER: tuple[ReviewRouteReason, ...] = (
    "support_sufficient",
    "support_insufficient",
    "hypothesis_counter_evidence_present",
    "review_counter_evidence_present",
    "evidence_gaps_present",
    "competing_explanations_present",
    "follow_up_suggestions_missing",
    "follow_up_suggestions_present",
    "round_limit_reached",
    "per_round_task_limit_exceeded",
    "total_task_limit_exceeded",
    "candidate_requirements_satisfied",
    "follow_up_capacity_available",
)
_REASON_INDEX = {reason: index for index, reason in enumerate(_REASON_ORDER)}


class ReviewRoutingDecision(TaskModel):
    """固定规则产生的路由事实内容，不执行对应路径。"""

    version: Literal["review-routing-v1"] = "review-routing-v1"
    status: Literal["recorded"] = "recorded"
    rule_version: RoutingRuleVersion = ROUTING_RULE_VERSION
    route: ReviewRoute
    reason_codes: Annotated[tuple[ReviewRouteReason, ...], Field(min_length=2, max_length=13)]
    round_number: int = Field(ge=1, le=3, strict=True)
    existing_task_count: int = Field(ge=0, le=9, strict=True)
    max_rounds: int = Field(ge=1, le=3, strict=True)
    max_tasks_per_round: int = Field(ge=1, le=3, strict=True)
    max_total_tasks: int = Field(ge=1, le=9, strict=True)
    remaining_rounds: int = Field(ge=0, le=2, strict=True)
    remaining_task_slots: int = Field(ge=0, le=9, strict=True)
    suggested_task_count: int = Field(ge=0, le=3, strict=True)
    follow_up_suggestions: tuple[FollowUpSuggestion, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def validate_decision(self) -> Self:
        reasons = self.reason_codes
        reason_set = set(reasons)
        if (
            len(reason_set) != len(reasons)
            or tuple(sorted(reasons, key=_REASON_INDEX.__getitem__)) != reasons
        ):
            raise ValueError("审查路由原因必须唯一并按固定顺序保存")
        if ("support_sufficient" in reason_set) == ("support_insufficient" in reason_set):
            raise ValueError("审查路由必须且只能保存一种支持充分性原因")
        if self.round_number > self.max_rounds:
            raise ValueError("当前轮次不能超过固定轮次上限")
        if self.existing_task_count > self.max_total_tasks:
            raise ValueError("既有任务数不能超过固定总任务上限")
        if self.remaining_rounds != self.max_rounds - self.round_number:
            raise ValueError("剩余轮次数与固定轮次上限不一致")
        if self.remaining_task_slots != self.max_total_tasks - self.existing_task_count:
            raise ValueError("剩余任务数与固定总任务上限不一致")
        if self.suggested_task_count != len(self.follow_up_suggestions):
            raise ValueError("补查建议计数与完整建议不一致")

        objection_reasons = {
            "hypothesis_counter_evidence_present",
            "review_counter_evidence_present",
            "evidence_gaps_present",
            "competing_explanations_present",
        }
        limit_reasons = {
            "round_limit_reached",
            "per_round_task_limit_exceeded",
            "total_task_limit_exceeded",
        }
        if self.suggested_task_count:
            if "follow_up_suggestions_present" not in reason_set:
                raise ValueError("存在完整补查建议时必须保存对应原因")
            if "follow_up_suggestions_missing" in reason_set:
                raise ValueError("有补查建议时不能标记建议缺失")
        elif (
            "follow_up_suggestions_missing" not in reason_set
            or "follow_up_suggestions_present" in reason_set
        ) and self.route != "candidate_evaluation":
            raise ValueError("没有补查建议时必须保存建议缺失原因")

        if self.route == "candidate_evaluation":
            if reason_set != {"support_sufficient", "candidate_requirements_satisfied"}:
                raise ValueError("候选评估只能由无异议的充分支持进入")
            if self.follow_up_suggestions:
                raise ValueError("候选评估不能携带补查建议")
        elif self.route == "follow_up":
            if (
                "support_insufficient" not in reason_set
                or "follow_up_capacity_available" not in reason_set
                or reason_set & limit_reasons
                or not self.follow_up_suggestions
                or self.remaining_rounds < 1
                or self.suggested_task_count > self.max_tasks_per_round
                or self.suggested_task_count > self.remaining_task_slots
            ):
                raise ValueError("补查路由必须有完整建议和足够轮次、单轮及总任务容量")
        else:
            if "candidate_requirements_satisfied" in reason_set or (
                "follow_up_capacity_available" in reason_set
            ):
                raise ValueError("等待人工不能同时标记自动路径已经满足")
            if "support_sufficient" in reason_set:
                if not reason_set & objection_reasons or self.follow_up_suggestions:
                    raise ValueError("支持充分时只有保留实质异议才能等待人工")
            elif self.follow_up_suggestions:
                if not reason_set & limit_reasons:
                    raise ValueError("存在补查建议时只有容量不足才能等待人工")
            elif "follow_up_suggestions_missing" not in reason_set:
                raise ValueError("支持不足且无补查建议时必须记录建议缺失")
        return self


class ReviewRoutingRecord(TaskModel):
    """确定性路由与开放假设、审查和写入尝试的不可变绑定。"""

    id: UUID
    investigation_run_id: UUID
    round_id: UUID
    open_hypothesis_id: UUID
    hypothesis_review_id: UUID
    investigation_attempt_id: UUID
    version: Literal["review-routing-v1"]
    status: Literal["recorded"]
    review_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision: ReviewRoutingDecision
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if (
            self.version != self.decision.version
            or self.status != self.decision.status
            or content_hash(self.decision) != self.content_sha256
        ):
            raise ValueError("审查路由记录的版本、状态或散列不一致")
        return self
