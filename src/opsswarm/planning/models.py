"""结构化处置计划、风险与验证值对象。"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictPlanningModel(BaseModel):
    """处置计划值对象共同约束。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ActionType(StrEnum):
    """确定性执行器未来可以识别的动作白名单。"""

    ROLLBACK_RELEASE = "rollback_release"


class TargetType(StrEnum):
    """动作目标类别。"""

    SERVICE = "service"


class RiskLevel(StrEnum):
    """动作风险等级。"""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class ActionPlanStatus(StrEnum):
    """计划从草案到审批终态的生命周期状态。"""

    DRAFT = "draft"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class VerificationOperator(StrEnum):
    """恢复验证支持的确定性比较操作。"""

    LESS_THAN = "less_than"


class RollbackReleaseParameters(StrictPlanningModel):
    """版本回滚动作唯一允许的参数集合。"""

    current_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    target_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )

    @model_validator(mode="after")
    def reject_noop_rollback(self) -> Self:
        if self.current_version == self.target_version:
            raise ValueError("回滚的当前版本和目标版本不能相同")
        return self


class VerificationRule(StrictPlanningModel):
    """动作完成后必须满足的最小恢复判据。"""

    signal: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9._-]*$")
    operator: VerificationOperator
    threshold: float = Field(ge=0, le=1)
    observation_window_seconds: int = Field(ge=10, le=3600)
    required_successful_samples: int = Field(ge=1, le=100)


class RiskDecision(StrictPlanningModel):
    """版本化风险策略对一个动作的确定性判定。"""

    risk_level: RiskLevel
    requires_human_approval: bool
    policy_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    rule_code: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_]*$")
    rationale: str = Field(min_length=1, max_length=500)


class ActionStep(StrictPlanningModel):
    """一个尚未授权、不可直接执行的白名单动作步骤。"""

    step_order: int = Field(ge=1, le=100)
    action_type: ActionType
    target_type: TargetType
    target_name: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    parameters: RollbackReleaseParameters
    risk_level: RiskLevel
    requires_human_approval: bool
    risk_rule_code: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_]*$",
    )
    risk_rationale: str = Field(min_length=1, max_length=500)
    verification: VerificationRule


class ActionPlan(StrictPlanningModel):
    """与精确版本绑定的结构化处置计划草案。"""

    version: int = Field(ge=1)
    status: ActionPlanStatus
    summary: str = Field(min_length=1, max_length=1000)
    risk_level: RiskLevel
    requires_human_approval: bool
    risk_policy_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    steps: tuple[ActionStep, ...] = Field(min_length=1, max_length=20)

    @field_validator("steps")
    @classmethod
    def require_contiguous_step_order(cls, value: tuple[ActionStep, ...]) -> tuple[ActionStep, ...]:
        orders = [item.step_order for item in value]
        if orders != list(range(1, len(value) + 1)):
            raise ValueError("处置步骤必须从 1 开始连续排序")
        return value

    @model_validator(mode="after")
    def enforce_plan_risk_summary(self) -> Self:
        highest = max(self.steps, key=lambda item: _RISK_RANK[item.risk_level]).risk_level
        if self.risk_level is not highest:
            raise ValueError("计划风险必须等于全部步骤中的最高风险")
        if self.requires_human_approval != any(item.requires_human_approval for item in self.steps):
            raise ValueError("计划审批要求必须与步骤审批要求一致")
        return self


_RISK_RANK = {
    RiskLevel.LOW: 1,
    RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3,
    RiskLevel.CRITICAL: 4,
}
