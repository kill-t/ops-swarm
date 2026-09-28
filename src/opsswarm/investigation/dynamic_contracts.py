"""v3 动态图阶段结果契约。"""

from typing import Self

from pydantic import Field, model_validator

from opsswarm.investigation.coordination.contracts import CoordinationPlanRecord
from opsswarm.investigation.review.contracts import HypothesisReviewRecord
from opsswarm.investigation.review_routing.contracts import ReviewRoutingRecord
from opsswarm.investigation.synthesis.contracts import OpenHypothesisRecord
from opsswarm.investigation.tasks.contracts import TaskModel
from opsswarm.investigation.tasks.results import SpecialistTaskResult


class FirstRoundInvestigationResult(TaskModel):
    """v3 首轮计划及其已提交专业结果，不代表最终调查结论。"""

    coordination_plan: CoordinationPlanRecord
    specialist_results: tuple[SpecialistTaskResult, ...] = Field(min_length=1, max_length=3)

    @model_validator(mode="after")
    def validate_plan_results(self) -> Self:
        assignments = self.coordination_plan.plan.assignment.tasks
        by_role = {item.report.specialist: item for item in self.specialist_results}
        if len(by_role) != len(self.specialist_results) or set(by_role) != {
            item.role for item in assignments
        }:
            raise ValueError("首轮专业结果必须与协调计划选择的角色完全一致")
        if any(
            by_role[item.role].evidence_snapshot_id != item.evidence_snapshot_id
            for item in assignments
        ):
            raise ValueError("首轮专业结果与协调计划的证据快照不一致")
        return self


class FirstRoundSynthesisResult(FirstRoundInvestigationResult):
    """首轮专业结果及其待审查开放假设，仍不是最终调查结论。"""

    open_hypothesis: OpenHypothesisRecord

    @model_validator(mode="after")
    def validate_hypothesis_source(self) -> Self:
        if (
            self.open_hypothesis.investigation_run_id != self.coordination_plan.investigation_run_id
            or self.open_hypothesis.round_id != self.coordination_plan.round_id
        ):
            raise ValueError("开放假设必须属于当前首轮协调计划")
        return self


class FirstRoundReviewResult(FirstRoundSynthesisResult):
    """首轮开放假设及其独立审查事实，仍不代表最终路由或结论。"""

    hypothesis_review: HypothesisReviewRecord

    @model_validator(mode="after")
    def validate_review_source(self) -> Self:
        if (
            self.hypothesis_review.investigation_run_id != self.open_hypothesis.investigation_run_id
            or self.hypothesis_review.round_id != self.open_hypothesis.round_id
            or self.hypothesis_review.open_hypothesis_id != self.open_hypothesis.id
        ):
            raise ValueError("独立审查必须属于当前开放假设")
        return self


class FirstRoundRoutingResult(FirstRoundReviewResult):
    """首轮审查及其确定性路由事实，仍不代表最终调查结论。"""

    review_route: ReviewRoutingRecord

    @model_validator(mode="after")
    def validate_route_source(self) -> Self:
        if (
            self.review_route.investigation_run_id != self.hypothesis_review.investigation_run_id
            or self.review_route.round_id != self.hypothesis_review.round_id
            or self.review_route.open_hypothesis_id != self.open_hypothesis.id
            or self.review_route.hypothesis_review_id != self.hypothesis_review.id
        ):
            raise ValueError("确定性路由必须属于当前独立审查")
        return self


class FollowUpPlanningResult(FirstRoundRoutingResult):
    """首轮路由及可选第二轮计划；任务仍未执行。"""

    follow_up_plan: CoordinationPlanRecord | None = None

    @model_validator(mode="after")
    def validate_follow_up_plan(self) -> Self:
        route = self.review_route
        if route.decision.route == "follow_up":
            if (
                self.follow_up_plan is None
                or self.follow_up_plan.investigation_run_id != route.investigation_run_id
                or self.follow_up_plan.source_review_route_id != route.id
                or self.follow_up_plan.source_call_id is not None
                or self.follow_up_plan.plan.assignment.round_number
                != route.decision.round_number + 1
            ):
                raise ValueError("补查路由必须绑定其下一轮协调计划")
        elif self.follow_up_plan is not None:
            raise ValueError("非补查路由不能携带后续协调计划")
        return self


class FollowUpExecutionResult(FollowUpPlanningResult):
    """首轮路由、可选第二轮计划及其已提交专业结果。"""

    follow_up_results: tuple[SpecialistTaskResult, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def validate_follow_up_results(self) -> Self:
        if self.review_route.decision.route != "follow_up":
            if self.follow_up_results:
                raise ValueError("非补查路由不能携带第二轮专业结果")
            return self
        if self.follow_up_plan is None:
            # 父契约会提供面向调用方的计划缺失错误，此处避免依赖无效字段。
            return self
        assignments = self.follow_up_plan.plan.assignment.tasks
        by_role = {item.report.specialist: item for item in self.follow_up_results}
        if len(by_role) != len(self.follow_up_results) or set(by_role) != {
            item.role for item in assignments
        }:
            raise ValueError("第二轮专业结果必须与补查计划选择的角色完全一致")
        if any(
            by_role[item.role].evidence_snapshot_id != item.evidence_snapshot_id
            for item in assignments
        ):
            raise ValueError("第二轮专业结果与补查计划的证据快照不一致")
        return self
