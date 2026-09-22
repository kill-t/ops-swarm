"""v3 动态图阶段结果契约。"""

from typing import Self

from pydantic import Field, model_validator

from opsswarm.investigation.coordination.contracts import CoordinationPlanRecord
from opsswarm.investigation.review.contracts import HypothesisReviewRecord
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
