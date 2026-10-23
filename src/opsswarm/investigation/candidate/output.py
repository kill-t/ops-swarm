"""候选提案和确定性评估值对象，不包含任何执行授权。"""

from typing import Annotated, Literal, Self

from pydantic import Field, field_validator, model_validator

from opsswarm.investigation.tasks.contracts import TaskModel
from opsswarm.planning.models import ActionStep, ActionType

CandidateItem = Annotated[str, Field(min_length=1, max_length=500)]
EvidenceId = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")]


class CandidateProposal(TaskModel):
    """模型只选择能力与证据，参数、风险和授权不在其输出范围。"""

    status: Literal["candidate", "unsupported"]
    action_type: ActionType | None = None
    rationale: str = Field(min_length=1, max_length=1000)
    evidence_ids: tuple[EvidenceId, ...] = Field(default=(), max_length=100)
    preconditions: tuple[CandidateItem, ...] = Field(default=(), max_length=10)
    unresolved_questions: tuple[CandidateItem, ...] = Field(default=(), max_length=10)

    @field_validator("evidence_ids", "preconditions", "unresolved_questions")
    @classmethod
    def require_unique_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("候选条目不能重复")
        return value

    @model_validator(mode="after")
    def validate_shape(self) -> Self:
        if self.status == "candidate":
            if self.action_type is None or not self.evidence_ids or not self.preconditions:
                raise ValueError("候选必须包含能力、证据引用和前置条件")
        elif self.action_type is not None:
            raise ValueError("不支持的候选不能携带能力标识")
        return self


class CandidateModelResult(TaskModel):
    """调用账本中的原始候选提案。"""

    kind: Literal["model"] = "model"
    version: Literal["candidate-model-result-v1"] = "candidate-model-result-v1"
    proposal: CandidateProposal


class CandidateEvaluation(TaskModel):
    """安全模板可行性评估，只能交给后续规划审查。"""

    version: Literal["candidate-evaluation-result-v1"] = "candidate-evaluation-result-v1"
    proposal: CandidateProposal
    disposition: Literal["planning_review", "await_human"]
    step: ActionStep | None = None
    reason_code: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9_]*$")
    execution_eligible: Literal[False] = False

    @model_validator(mode="after")
    def validate_step(self) -> Self:
        if self.disposition == "planning_review":
            if (
                self.proposal.status != "candidate"
                or self.step is None
                or self.step.action_type != self.proposal.action_type
            ):
                raise ValueError("规划审查需要与候选一致的确定性步骤")
        elif self.step is not None:
            raise ValueError("等待人工时不能携带可用步骤")
        return self
