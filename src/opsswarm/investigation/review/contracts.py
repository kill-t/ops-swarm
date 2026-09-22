"""开放假设独立审查的模型提案与持久事实契约。"""

from dataclasses import dataclass
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, field_validator, model_validator

from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.tasks.contracts import TaskModel

EvidenceId = Annotated[
    str,
    Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$"),
]
EvidenceIds = Annotated[tuple[EvidenceId, ...], Field(max_length=100)]
ReviewItems = Annotated[tuple[str, ...], Field(max_length=20)]
SuggestedDirection = Literal["evaluate_for_routing", "follow_up", "human_review"]


class FollowUpSuggestion(TaskModel):
    """审查者提出的有限补查意图，不创建业务任务。"""

    role: EvidenceKind
    question: str = Field(min_length=1, max_length=500)
    rationale: str = Field(min_length=1, max_length=500)


class HypothesisReviewContent(TaskModel):
    """模型提案与持久审查事实共用的有限语义。"""

    support_is_sufficient: bool = Field(strict=True)
    rationale: str = Field(min_length=1, max_length=1000)
    accepted_supporting_evidence_ids: EvidenceIds = ()
    counter_evidence_ids: EvidenceIds = ()
    evidence_gaps: ReviewItems = ()
    competing_explanations: ReviewItems = ()
    suggested_direction: SuggestedDirection
    follow_up_suggestions: tuple[FollowUpSuggestion, ...] = Field(default=(), max_length=3)

    @field_validator("accepted_supporting_evidence_ids", "counter_evidence_ids")
    @classmethod
    def require_unique_evidence(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("独立审查不能重复引用同一类证据")
        return value

    @field_validator("evidence_gaps", "competing_explanations")
    @classmethod
    def require_unique_review_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(item.strip() for item in value)
        if any(not item or len(item) > 500 for item in normalized):
            raise ValueError("审查条目长度必须在 1 到 500 之间")
        if len(set(normalized)) != len(normalized):
            raise ValueError("独立审查不能包含重复条目")
        return normalized

    @model_validator(mode="after")
    def validate_review_shape(self) -> Self:
        if set(self.accepted_supporting_evidence_ids) & set(self.counter_evidence_ids):
            raise ValueError("同一证据不能同时被接受为支持证据和反证")
        if len({item.role for item in self.follow_up_suggestions}) != len(
            self.follow_up_suggestions
        ):
            raise ValueError("同一专业角色最多提出一个补查建议")
        if self.suggested_direction == "evaluate_for_routing":
            if not self.support_is_sufficient or not self.accepted_supporting_evidence_ids:
                raise ValueError("建议进入确定性路由评估时必须确认存在充分支持证据")
            if self.follow_up_suggestions:
                raise ValueError("建议进入确定性路由评估时不能同时提出补查")
        elif self.suggested_direction == "follow_up":
            if self.support_is_sufficient or not self.follow_up_suggestions:
                raise ValueError("建议补查时必须认为支持不足并提出至少一个有限补查意图")
        elif self.support_is_sufficient or self.follow_up_suggestions:
            raise ValueError("建议人工判断时不能宣称支持充分或同时提出补查")
        if not self.support_is_sufficient and not (
            self.counter_evidence_ids or self.evidence_gaps or self.competing_explanations
        ):
            raise ValueError("认为支持不足时必须保留反证、证据缺口或竞争性解释")
        return self


class HypothesisReviewProposal(HypothesisReviewContent):
    """模型提出的独立审查意见，不包含路由决定或执行授权。"""

    version: Literal["hypothesis-review-proposal-v1"] = "hypothesis-review-proposal-v1"


class HypothesisReview(HypothesisReviewContent):
    """系统注入记录状态后的不可变审查事实。"""

    version: Literal["hypothesis-review-v1"] = "hypothesis-review-v1"
    status: Literal["recorded"] = "recorded"

    @classmethod
    def from_proposal(cls, proposal: HypothesisReviewProposal) -> Self:
        return cls(**proposal.model_dump(exclude={"version"}))


class ReviewModelResult(TaskModel):
    """调用账本保存的审查提案，供跨父尝试恢复。"""

    kind: Literal["model"] = "model"
    version: Literal["hypothesis-review-result-v1"] = "hypothesis-review-result-v1"
    proposal: HypothesisReviewProposal


class HypothesisReviewRecord(TaskModel):
    """审查事实与开放假设、父尝试和成功模型调用的绑定。"""

    id: UUID
    investigation_run_id: UUID
    round_id: UUID
    open_hypothesis_id: UUID
    investigation_attempt_id: UUID
    source_call_id: UUID
    version: Literal["hypothesis-review-v1"]
    status: Literal["recorded"]
    review: HypothesisReview
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: AwareDatetime

    @model_validator(mode="after")
    def validate_record(self) -> Self:
        if (
            self.version != self.review.version
            or self.status != self.review.status
            or content_hash(self.review) != self.content_sha256
        ):
            raise ValueError("独立审查记录的版本、状态或散列不一致")
        return self


@dataclass(frozen=True, slots=True)
class ReviewProposalCall:
    call_id: UUID
    proposal: HypothesisReviewProposal
    reused: bool
