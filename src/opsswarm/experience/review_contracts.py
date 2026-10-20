"""经验审核命令、不可变决定与确认版本的公开契约。"""

from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from opsswarm.experience.contracts import ExperienceDraft, ExperienceModel, Hash

REVIEW_POLICY_VERSION: Literal["experience-review-v1"] = "experience-review-v1"


class ReviewDecision(StrEnum):
    CONFIRM = "confirm"
    REJECT = "reject"


class ReviewCommand(ExperienceModel):
    idempotency_key: str = Field(
        min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    expected_candidate_version: int = Field(ge=1, le=2**31 - 1)
    expected_candidate_content_hash: Hash
    expected_review_version: int = Field(ge=0, le=2**31 - 1)
    decision: ReviewDecision
    reason: str = Field(min_length=1, max_length=1000)


class ReviewView(ExperienceModel):
    id: UUID
    candidate_id: UUID
    candidate_version: int = Field(ge=1)
    candidate_content_hash: Hash
    source_hash: Hash
    review_version: Literal[1]
    policy_version: Literal["experience-review-v1"]
    decision: ReviewDecision
    reviewed_by_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=100)
    command_hash: Hash
    reason: str = Field(min_length=1, max_length=1000)
    reviewed_at: AwareDatetime


class ConfirmedExperienceContent(ExperienceModel):
    schema_version: Literal["confirmed-experience-v1"] = "confirmed-experience-v1"
    candidate_snapshot: ExperienceDraft
    confirmation_notice: Literal[
        "该候选已由可信工程师确认为历史参考；使用前必须重新调查当前事故，历史审批不授予当前执行权限。"
    ] = (
        "该候选已由可信工程师确认为历史参考；使用前必须重新调查当前事故，"
        "历史审批不授予当前执行权限。"
    )


class EntryView(ExperienceModel):
    id: UUID
    review_id: UUID
    candidate_id: UUID
    candidate_version: int = Field(ge=1)
    candidate_content_hash: Hash
    source_hash: Hash
    version: Literal[1]
    status: Literal["confirmed"]
    content: ConfirmedExperienceContent
    content_hash: Hash
    created_at: AwareDatetime


class ReviewState(ExperienceModel):
    candidate_id: UUID
    candidate_version: int = Field(ge=1)
    candidate_content_hash: Hash
    review_version: Literal[0, 1]
    status: Literal["pending_review", "confirmed", "rejected"]
    review: ReviewView | None
    entry_id: UUID | None


class ReviewResult(ExperienceModel):
    applied: bool
    review: ReviewView
    entry: EntryView | None
