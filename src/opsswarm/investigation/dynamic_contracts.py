"""v3 动态图阶段结果契约。"""

from typing import Self

from pydantic import Field, model_validator

from opsswarm.investigation.coordination.contracts import CoordinationPlanRecord
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
