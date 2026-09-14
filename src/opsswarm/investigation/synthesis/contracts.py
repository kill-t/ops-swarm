"""v3 协调综合的开放假设与调用来源契约。"""

from dataclasses import dataclass
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.tasks.contracts import TaskModel

EvidenceIds = Annotated[
    tuple[str, ...],
    Field(max_length=100),
]


class OpenHypothesisContent(TaskModel):
    """模型提案与持久事实共用的开放假设语义约束。"""

    statement: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: EvidenceIds = Field(min_length=1)
    counter_evidence_ids: EvidenceIds = ()
    unresolved_questions: tuple[str, ...] = Field(min_length=1, max_length=20)

    @field_validator("supporting_evidence_ids", "counter_evidence_ids")
    @classmethod
    def require_unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("开放假设不能重复引用同一类证据")
        return value

    @field_validator("unresolved_questions")
    @classmethod
    def require_unique_questions(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item or len(item) > 500 for item in normalized):
            raise ValueError("未解问题长度必须在 1 到 500 之间")
        if len(set(normalized)) != len(normalized):
            raise ValueError("开放假设不能包含重复未解问题")
        return normalized

    @model_validator(mode="after")
    def require_disjoint_evidence(self) -> Self:
        if set(self.supporting_evidence_ids) & set(self.counter_evidence_ids):
            raise ValueError("同一证据不能同时标记为支持和反证")
        return self


class OpenHypothesisProposal(OpenHypothesisContent):
    """模型提出的自由文本原因判断，不包含审查状态或执行建议。"""

    version: Literal["open-hypothesis-proposal-v1"] = "open-hypothesis-proposal-v1"


class OpenHypothesis(OpenHypothesisContent):
    """系统注入待审查状态后的不可变开放假设。"""

    version: Literal["open-hypothesis-v1"] = "open-hypothesis-v1"
    status: Literal["pending_review"] = "pending_review"
    statement: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    supporting_evidence_ids: EvidenceIds = Field(min_length=1)
    counter_evidence_ids: EvidenceIds = ()
    unresolved_questions: tuple[str, ...] = Field(min_length=1, max_length=20)

    @classmethod
    def from_proposal(cls, proposal: OpenHypothesisProposal) -> Self:
        return cls(**proposal.model_dump(exclude={"version"}))


class SynthesisModelResult(TaskModel):
    """调用账本保存的开放假设提案，供跨父尝试恢复。"""

    kind: Literal["model"] = "model"
    version: Literal["open-hypothesis-synthesis-result-v1"] = "open-hypothesis-synthesis-result-v1"
    proposal: OpenHypothesisProposal


class OpenHypothesisRecord(TaskModel):
    """开放假设与轮次、父尝试和成功综合调用的绑定。"""

    id: UUID
    investigation_run_id: UUID
    round_id: UUID
    investigation_attempt_id: UUID
    source_call_id: UUID
    version: Literal["open-hypothesis-v1"]
    status: Literal["pending_review"]
    hypothesis: OpenHypothesis
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if (
            self.version != self.hypothesis.version
            or self.status != self.hypothesis.status
            or content_hash(self.hypothesis) != self.content_sha256
        ):
            raise ValueError("开放假设记录的版本、状态或散列不一致")
        return self


@dataclass(frozen=True, slots=True)
class SynthesisProposalCall:
    call_id: UUID
    proposal: OpenHypothesisProposal
    reused: bool
