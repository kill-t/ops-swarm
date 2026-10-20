"""经验生成的公开快照与仅供 Worker 使用的租约身份。"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from opsswarm.experience.contracts import ExperienceDraft, ExperienceSource


class AttemptView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    number: int = Field(ge=1, le=3)
    worker_id: str
    status: Literal["running", "succeeded", "failed", "lease_expired"]
    started_at: AwareDatetime
    lease_expires_at: AwareDatetime
    finished_at: AwareDatetime | None
    error_code: str | None


class JobView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    report_id: UUID
    policy_version: Literal["report-fact-draft-v1"]
    source: ExperienceSource
    source_hash: str
    requested_by_id: str
    authorization_id: UUID
    status: Literal["queued", "running", "succeeded", "failed"]
    attempt_count: int = Field(ge=0, le=3)
    max_attempts: Literal[3]
    attempts: tuple[AttemptView, ...] = Field(max_length=3)
    candidate_id: UUID | None
    last_error_code: str | None
    created_at: AwareDatetime
    updated_at: AwareDatetime
    completed_at: AwareDatetime | None


class CandidateView(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: UUID
    job_id: UUID
    version: Literal[1]
    status: Literal["pending_review"]
    source_hash: str
    content: ExperienceDraft
    content_hash: str
    created_at: AwareDatetime


@dataclass(frozen=True)
class ExperienceClaim:
    job_id: UUID
    attempt_id: UUID
    lease_token: UUID
    lease_expires_at: datetime
