"""处置计划持久化读取模型。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from opsswarm.planning.models import (
    ActionParameters,
    ActionPlan,
    ActionPlanStatus,
    ActionStep,
    ActionType,
    RiskLevel,
    StrictPlanningModel,
    TargetType,
    VerificationRule,
)


class StoredActionStep(StrictPlanningModel):
    """带稳定数据库标识的处置步骤。"""

    id: UUID
    step_order: int
    action_type: ActionType
    target_type: TargetType
    target_name: str
    parameters: ActionParameters
    risk_level: RiskLevel
    requires_human_approval: bool
    risk_rule_code: str
    risk_rationale: str
    verification: VerificationRule
    created_at: datetime


class StoredActionPlan(StrictPlanningModel):
    """绑定调查、假设及精确版本的处置计划。"""

    id: UUID
    investigation_run_id: UUID
    hypothesis_id: UUID
    version: int
    status: ActionPlanStatus
    summary: str
    risk_level: RiskLevel
    requires_human_approval: bool
    risk_policy_version: str
    steps: tuple[StoredActionStep, ...]
    created_at: datetime

    def to_domain(self) -> ActionPlan:
        """去除数据库标识和时间，恢复可重算风险及摘要的领域计划。"""

        return ActionPlan(
            version=self.version,
            status=self.status,
            summary=self.summary,
            risk_level=self.risk_level,
            requires_human_approval=self.requires_human_approval,
            risk_policy_version=self.risk_policy_version,
            steps=tuple(
                ActionStep.model_validate(step.model_dump(exclude={"id", "created_at"}))
                for step in self.steps
            ),
        )


@dataclass(frozen=True, slots=True)
class ActionPlanCreation:
    """区分首次生成和幂等重复请求。"""

    plan: StoredActionPlan
    created: bool
