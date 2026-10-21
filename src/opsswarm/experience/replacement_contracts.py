"""可信替代命令与不可变直接后继事实，不重编号确认正文。"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from opsswarm.experience.contracts import ExperienceModel, Hash

REPLACEMENT_POLICY_VERSION: Literal["experience-replacement-v1"] = "experience-replacement-v1"


class ReplaceCommand(ExperienceModel):
    idempotency_key: str = Field(
        min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    expected_entry_version: int = Field(ge=1, le=2**31 - 1)
    expected_entry_content_hash: Hash
    expected_state_version: int = Field(ge=1, le=2**31 - 1)
    target_entry_id: UUID
    expected_target_version: int = Field(ge=1, le=2**31 - 1)
    expected_target_content_hash: Hash
    expected_target_state_version: int = Field(ge=1, le=2**31 - 1)
    reason: str = Field(min_length=1, max_length=1000)


class ReplacementView(ExperienceModel):
    id: UUID
    entry_id: UUID
    entry_version: Literal[1]
    entry_content_hash: Hash
    source_hash: Hash
    target_entry_id: UUID
    target_entry_version: Literal[1]
    target_content_hash: Hash
    target_source_hash: Hash
    state_version: Literal[2]
    policy_version: Literal["experience-replacement-v1"]
    replaced_by_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=100)
    command_hash: Hash
    reason: str = Field(min_length=1, max_length=1000)
    replaced_at: AwareDatetime


class ReplaceResult(ExperienceModel):
    applied: bool
    replacement: ReplacementView
