"""历史确认工件之外的有效性、停用命令与有限检索响应。"""

from typing import Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from opsswarm.experience.contracts import ExperienceModel, Hash
from opsswarm.experience.replacement_contracts import ReplacementView
from opsswarm.investigation.models import InvestigationDataSourceMode, RootCause

LIFECYCLE_POLICY_VERSION: Literal["experience-lifecycle-v1"] = "experience-lifecycle-v1"
SEARCH_POLICY_VERSION: Literal["experience-search-v2"] = "experience-search-v2"


class DisableCommand(ExperienceModel):
    idempotency_key: str = Field(
        min_length=8, max_length=100, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$"
    )
    expected_entry_version: int = Field(ge=1, le=2**31 - 1)
    expected_entry_content_hash: Hash
    expected_state_version: int = Field(ge=1, le=2**31 - 1)
    reason: str = Field(min_length=1, max_length=1000)


class DisablementView(ExperienceModel):
    id: UUID
    entry_id: UUID
    entry_version: Literal[1]
    entry_content_hash: Hash
    source_hash: Hash
    state_version: Literal[2]
    policy_version: Literal["experience-lifecycle-v1"]
    disabled_by_id: str = Field(min_length=1, max_length=100)
    authorization_id: UUID
    idempotency_key: str = Field(min_length=8, max_length=100)
    command_hash: Hash
    reason: str = Field(min_length=1, max_length=1000)
    disabled_at: AwareDatetime


class EntryState(ExperienceModel):
    entry_id: UUID
    entry_version: Literal[1]
    entry_content_hash: Hash
    state_version: Literal[1, 2]
    status: Literal["active", "disabled", "replaced"]
    disablement: DisablementView | None
    replacement: ReplacementView | None = None


class DisableResult(ExperienceModel):
    applied: bool
    state: EntryState


class ExperienceSearchQuery(ExperienceModel):
    preferred_fault_tag: RootCause | None = None


class SearchScope(ExperienceModel):
    run_id: UUID
    incident_id: UUID
    service_name: str = Field(min_length=1, max_length=100)
    environment: Literal["local_fault_lab"] = "local_fault_lab"
    data_source_mode: InvestigationDataSourceMode


class ExperienceSearchItem(ExperienceModel):
    entry_id: UUID
    entry_version: Literal[1]
    content_hash: Hash
    source_hash: Hash
    source_incident_id: UUID
    state_version: Literal[1] = 1
    historical_summary: str = Field(min_length=1, max_length=2000)
    confirmation_notice: str = Field(min_length=1, max_length=500)
    fault_tags: tuple[str, ...] = Field(min_length=1, max_length=5)
    has_refuting_evidence: bool
    evidence_count: int = Field(ge=1, le=20)
    confirmed_at: AwareDatetime


class ExperienceSearchResult(ExperienceModel):
    policy_version: Literal["experience-search-v2"] = SEARCH_POLICY_VERSION
    scope: SearchScope
    preferred_fault_tag: RootCause | None
    retrieved_at: AwareDatetime
    items: tuple[ExperienceSearchItem, ...] = Field(max_length=3)
