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


class SecondRoundSynthesisResult(FollowUpExecutionResult):
    """补查执行结果及其可选第二轮开放假设，仍不是最终调查结论。"""

    follow_up_hypothesis: OpenHypothesisRecord | None = None

    @model_validator(mode="after")
    def validate_follow_up_hypothesis(self) -> Self:
        if self.review_route.decision.route != "follow_up":
            if self.follow_up_hypothesis is not None:
                raise ValueError("非补查路由不能携带第二轮开放假设")
            return self
        if self.follow_up_plan is None:
            # 父契约负责报告计划缺失；此处避免读取无效字段。
            return self
        if (
            self.follow_up_hypothesis is None
            or self.follow_up_hypothesis.investigation_run_id
            != self.follow_up_plan.investigation_run_id
            or self.follow_up_hypothesis.round_id != self.follow_up_plan.round_id
        ):
            raise ValueError("补查路由必须携带绑定第二轮计划的开放假设")
        return self


class SecondRoundReviewResult(SecondRoundSynthesisResult):
    """第二轮开放假设及其可选独立审查，仍不代表最终路由或结论。"""

    follow_up_hypothesis_review: HypothesisReviewRecord | None = None

    @model_validator(mode="after")
    def validate_follow_up_review(self) -> Self:
        if self.review_route.decision.route != "follow_up":
            if self.follow_up_hypothesis_review is not None:
                raise ValueError("非补查路由不能携带第二轮独立审查")
            return self
        if self.follow_up_hypothesis is None:
            # 父契约负责报告假设缺失；此处避免读取无效字段。
            return self
        review = self.follow_up_hypothesis_review
        if (
            review is None
            or review.investigation_run_id != self.follow_up_hypothesis.investigation_run_id
            or review.round_id != self.follow_up_hypothesis.round_id
            or review.open_hypothesis_id != self.follow_up_hypothesis.id
        ):
            raise ValueError("补查路由必须携带绑定第二轮开放假设的独立审查")
        return self


class SecondRoundRoutingResult(SecondRoundReviewResult):
    """第二轮独立审查及其确定性路由，仍不执行下一路径。"""

    follow_up_review_route: ReviewRoutingRecord | None = None

    @model_validator(mode="after")
    def validate_follow_up_route(self) -> Self:
        if self.review_route.decision.route != "follow_up":
            if self.follow_up_review_route is not None:
                raise ValueError("非补查首轮路由不能携带第二轮确定性路由")
            return self
        review = self.follow_up_hypothesis_review
        route = self.follow_up_review_route
        if review is None:
            # 父契约负责报告审查缺失；此处避免读取无效字段。
            return self
        if (
            route is None
            or route.investigation_run_id != review.investigation_run_id
            or route.round_id != review.round_id
            or route.open_hypothesis_id != review.open_hypothesis_id
            or route.hypothesis_review_id != review.id
        ):
            raise ValueError("补查首轮路由必须携带绑定第二轮审查的确定性路由")
        return self


class ThirdRoundPlanningResult(SecondRoundRoutingResult):
    """第二轮路由及其可选第三轮计划，尚未执行第三轮任务。"""

    third_round_plan: CoordinationPlanRecord | None = None

    @model_validator(mode="after")
    def validate_third_round_plan(self) -> Self:
        route = self.follow_up_review_route
        plan = self.third_round_plan
        if route is None or route.decision.route != "follow_up":
            if plan is not None:
                raise ValueError("非补查第二轮路由不能携带第三轮计划")
            return self
        if (
            plan is None
            or plan.investigation_run_id != route.investigation_run_id
            or plan.source_review_route_id != route.id
            or plan.source_call_id is not None
            or plan.plan.assignment.round_number != route.decision.round_number + 1
            or plan.plan.assignment.round_number != 3
        ):
            raise ValueError("补查第二轮路由必须携带绑定该路由的第三轮计划")
        return self


class ThirdRoundExecutionResult(ThirdRoundPlanningResult):
    """可选第三轮计划及其已提交专业结果，仍不是最终调查结论。"""

    third_round_results: tuple[SpecialistTaskResult, ...] = Field(default=(), max_length=3)

    @model_validator(mode="after")
    def validate_third_round_results(self) -> Self:
        plan = self.third_round_plan
        if plan is None:
            if self.third_round_results:
                raise ValueError("没有第三轮计划时不能携带第三轮专业结果")
            return self
        assignments = plan.plan.assignment.tasks
        by_role = {item.report.specialist: item for item in self.third_round_results}
        if len(by_role) != len(self.third_round_results) or set(by_role) != {
            item.role for item in assignments
        }:
            raise ValueError("第三轮专业结果必须与第三轮计划选择的角色完全一致")
        if any(
            by_role[item.role].evidence_snapshot_id != item.evidence_snapshot_id
            for item in assignments
        ):
            raise ValueError("第三轮专业结果与第三轮计划的证据快照不一致")
        return self


class ThirdRoundSynthesisResult(ThirdRoundExecutionResult):
    """第三轮专业结果及其可选开放假设，仍未经过独立审查。"""

    third_round_hypothesis: OpenHypothesisRecord | None = None

    @model_validator(mode="after")
    def validate_third_round_hypothesis(self) -> Self:
        plan = self.third_round_plan
        if plan is None:
            if self.third_round_hypothesis is not None:
                raise ValueError("没有第三轮计划时不能携带第三轮开放假设")
            return self
        if (
            self.third_round_hypothesis is None
            or self.third_round_hypothesis.investigation_run_id != plan.investigation_run_id
            or self.third_round_hypothesis.round_id != plan.round_id
        ):
            raise ValueError("第三轮开放假设必须绑定第三轮协调计划")
        return self


class ThirdRoundReviewResult(ThirdRoundSynthesisResult):
    """第三轮开放假设及其可选独立审查，仍不执行后续路由。"""

    third_round_hypothesis_review: HypothesisReviewRecord | None = None

    @model_validator(mode="after")
    def validate_third_round_review(self) -> Self:
        hypothesis = self.third_round_hypothesis
        if self.third_round_plan is None:
            if self.third_round_hypothesis_review is not None:
                raise ValueError("没有第三轮计划时不能携带第三轮独立审查")
            return self
        review = self.third_round_hypothesis_review
        if (
            hypothesis is None
            or review is None
            or review.investigation_run_id != hypothesis.investigation_run_id
            or review.round_id != hypothesis.round_id
            or review.open_hypothesis_id != hypothesis.id
        ):
            raise ValueError("第三轮独立审查必须绑定第三轮开放假设")
        return self


class ThirdRoundRoutingResult(ThirdRoundReviewResult):
    """第三轮独立审查及其最大轮次停止路由，不执行对应后续路径。"""

    third_round_review_route: ReviewRoutingRecord | None = None

    @model_validator(mode="after")
    def validate_third_round_route(self) -> Self:
        if self.third_round_plan is None:
            if self.third_round_review_route is not None:
                raise ValueError("没有第三轮计划时不能携带第三轮确定性路由")
            return self
        review = self.third_round_hypothesis_review
        route = self.third_round_review_route
        if review is None:
            # 父契约负责报告审查缺失；此处避免读取无效字段。
            return self
        if (
            route is None
            or route.investigation_run_id != review.investigation_run_id
            or route.round_id != review.round_id
            or route.open_hypothesis_id != review.open_hypothesis_id
            or route.hypothesis_review_id != review.id
            or route.review_sha256 != review.content_sha256
            or route.decision.round_number != 3
            or route.decision.remaining_rounds != 0
            or route.decision.route == "follow_up"
        ):
            raise ValueError("第三轮确定性路由必须绑定第三轮审查并停止自动补查")
        return self
