"""结构化处置计划、风险与验证值对象。"""

from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opsswarm.investigation.models import HypothesisStatus, Recommendation, RootCause


class StrictPlanningModel(BaseModel):
    """处置计划值对象共同约束。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class ActionType(StrEnum):
    """确定性执行器未来可以识别的动作白名单。"""

    ROLLBACK_RELEASE = "rollback_release"
    ROLLBACK_CONFIGURATION = "rollback_configuration"
    RESTORE_DEPENDENCY = "restore_dependency"


class ConfigurationKey(StrEnum):
    """第一版允许回滚的运行配置键白名单。"""

    DATABASE_POOL_SIZE = "database_pool_size"


class DependencyName(StrEnum):
    """允许执行恢复动作的依赖白名单。"""

    REDIS = "redis"


class ActionPlanCapabilityStatus(StrEnum):
    """确定性计划模板对调查建议的支持状态。"""

    AVAILABLE = "available"
    UNSUPPORTED = "unsupported"
    NOT_APPLICABLE = "not_applicable"


class ActionPlanCapabilityReason(StrEnum):
    """能力判断的稳定机器可读原因。"""

    TEMPLATE_AVAILABLE = "template_available"
    HYPOTHESIS_NOT_SUPPORTED = "hypothesis_not_supported"
    TEMPLATE_NOT_IMPLEMENTED = "template_not_implemented"
    RECOMMENDATION_ROOT_CAUSE_MISMATCH = "recommendation_root_cause_mismatch"
    CONTINUE_INVESTIGATION = "continue_investigation"


class ActionPlanCapability(StrictPlanningModel):
    """一次调查结论是否能进入确定性计划模板。"""

    root_cause: RootCause
    recommendation: Recommendation
    hypothesis_status: HypothesisStatus
    status: ActionPlanCapabilityStatus
    action_type: ActionType | None = None
    template_version: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    requires_human_approval: bool
    can_create: bool
    reason_code: ActionPlanCapabilityReason
    explanation: str = Field(min_length=1, max_length=500)

    @model_validator(mode="after")
    def enforce_capability_shape(self) -> Self:
        """模板、审批与可创建状态必须能够由输入事实重算。"""

        if self.status is ActionPlanCapabilityStatus.AVAILABLE:
            if self.action_type is None or self.template_version is None:
                raise ValueError("available 能力必须携带动作类型和模板版本")
        elif self.action_type is not None or self.template_version is not None:
            raise ValueError("不可用能力不能携带动作类型或模板版本")
        if self.requires_human_approval != self.recommendation.requires_human_approval:
            raise ValueError("能力审批要求必须与调查建议一致")
        expected_can_create = (
            self.status is ActionPlanCapabilityStatus.AVAILABLE
            and self.hypothesis_status is HypothesisStatus.SUPPORTED
        )
        if self.can_create != expected_can_create:
            raise ValueError("计划可创建状态必须同时满足模板可用和假设已支持")
        return self


class TargetType(StrEnum):
    """动作目标类别。"""

    SERVICE = "service"
    DEPENDENCY = "dependency"


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


class RollbackConfigurationParameters(StrictPlanningModel):
    """数据库连接池配置回滚唯一允许的严格整数参数集合。"""

    key: ConfigurationKey
    current_value: int = Field(strict=True, ge=1, le=1000)
    target_value: int = Field(strict=True, ge=1, le=1000)

    @model_validator(mode="after")
    def reject_noop_rollback(self) -> Self:
        if self.current_value == self.target_value:
            raise ValueError("配置回滚的当前值和目标值不能相同")
        return self


class RestoreDependencyParameters(StrictPlanningModel):
    """Redis 依赖恢复唯一允许的布尔状态迁移。"""

    dependency: DependencyName
    expected_available: bool = Field(strict=True)
    target_available: bool = Field(strict=True)

    @model_validator(mode="after")
    def require_unavailable_to_available(self) -> Self:
        if self.expected_available or not self.target_available:
            raise ValueError("依赖恢复只允许把 redis 从不可用恢复为可用")
        return self


ActionParameters = (
    RollbackReleaseParameters | RollbackConfigurationParameters | RestoreDependencyParameters
)


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
    parameters: ActionParameters
    risk_level: RiskLevel
    requires_human_approval: bool
    risk_rule_code: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_]*$",
    )
    risk_rationale: str = Field(min_length=1, max_length=500)
    verification: VerificationRule

    @model_validator(mode="after")
    def enforce_action_parameters(self) -> Self:
        """动作类型必须使用自己的精确参数模型，禁止跨模板借用字段。"""

        expected_type = {
            ActionType.ROLLBACK_RELEASE: RollbackReleaseParameters,
            ActionType.ROLLBACK_CONFIGURATION: RollbackConfigurationParameters,
            ActionType.RESTORE_DEPENDENCY: RestoreDependencyParameters,
        }[self.action_type]
        if not isinstance(self.parameters, expected_type):
            raise ValueError("动作类型与参数模型不匹配")
        return self


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
