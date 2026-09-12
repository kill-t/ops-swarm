"""协调 Agent 只能提出有限任务意图，系统范围由确定性代码注入。"""

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from opsswarm.investigation.coordination.hashing import coordination_hash
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.tasks.contracts import RoundAssignment, TaskModel

CoordinationPolicyVersion = Literal["dynamic-coordination-v1"]
COORDINATION_POLICY_VERSION: CoordinationPolicyVersion = "dynamic-coordination-v1"


class CoordinationPolicy(TaskModel):
    """首次动态计划写入时固定的轮次与任务资源上限。"""

    version: CoordinationPolicyVersion = COORDINATION_POLICY_VERSION
    max_rounds: int = Field(default=3, ge=1, le=3, strict=True)
    max_tasks_per_round: int = Field(default=3, ge=1, le=3, strict=True)
    max_total_tasks: int = Field(default=6, ge=1, le=9, strict=True)
    max_attempts_per_task: int = Field(default=3, ge=1, le=3, strict=True)


class ProposedTask(TaskModel):
    """模型可决定的任务部分，不包含服务、时间戳、快照或授权。"""

    task_key: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,63}$")
    role: EvidenceKind
    goal: str = Field(min_length=1, max_length=1000)
    lookback_seconds: int = Field(ge=60, le=21600, strict=True)


class CoordinationProposal(TaskModel):
    """协调模型的结构化输出；通过编译前不能创建业务任务。"""

    version: Literal["coordination-proposal-v1"] = "coordination-proposal-v1"
    round_number: int = Field(ge=1, le=3, strict=True)
    goal: str = Field(min_length=1, max_length=1000)
    tasks: Annotated[tuple[ProposedTask, ...], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def validate_tasks(self) -> Self:
        if len({task.task_key for task in self.tasks}) != len(self.tasks):
            raise ValueError("同一提案不能包含重复任务键")
        if len({task.role for task in self.tasks}) != len(self.tasks):
            raise ValueError("首版每轮同一专业角色最多分配一个任务")
        return self


class CompiledCoordinationPlan(TaskModel):
    """保存提案及其确定性分派结果，便于核对输入与业务事实。"""

    version: Literal["compiled-coordination-plan-v1"] = "compiled-coordination-plan-v1"
    policy_version: CoordinationPolicyVersion
    proposal: CoordinationProposal
    assignment: RoundAssignment


class CoordinationPolicyRecord(TaskModel):
    """一次运行固定且不可变的完整协调策略。"""

    investigation_run_id: UUID
    version: CoordinationPolicyVersion
    policy: CoordinationPolicy
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if (
            self.version != self.policy.version
            or coordination_hash(self.policy) != self.content_sha256
        ):
            raise ValueError("协调策略记录的版本或散列不一致")
        return self


class CoordinationPlanRecord(TaskModel):
    """协调计划与实际业务轮次的不可变绑定。"""

    round_id: UUID
    investigation_run_id: UUID
    investigation_attempt_id: UUID
    version: Literal["compiled-coordination-plan-v1"]
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan: CompiledCoordinationPlan
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if self.version != self.plan.version or coordination_hash(self.plan) != self.content_sha256:
            raise ValueError("协调计划记录的版本或散列不一致")
        return self
