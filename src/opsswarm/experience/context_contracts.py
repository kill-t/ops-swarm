"""首轮协调使用的有界历史经验选择，独立于当前有效状态。"""

from typing import Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from opsswarm.experience.context_policy import CoordinationExperiencePolicy
from opsswarm.experience.contracts import ExperienceModel
from opsswarm.experience.lifecycle_contracts import SearchScope
from opsswarm.experience.review_contracts import EntryView


class ExperienceContextItem(ExperienceModel):
    source_incident_id: UUID
    entry: EntryView


class ExperienceContextSelection(ExperienceModel):
    id: UUID
    policy_version: CoordinationExperiencePolicy = "coordination-experience-v1"
    search_policy_version: Literal["experience-search-v2"] = "experience-search-v2"
    scope: SearchScope
    selected_at: AwareDatetime
    items: tuple[ExperienceContextItem, ...] = Field(max_length=3)

    @model_validator(mode="after")
    def validate_scope(self) -> Self:
        if len({item.entry.id for item in self.items}) != len(self.items):
            raise ValueError("经验上下文不能重复引用同一工件")
        for item in self.items:
            draft = item.entry.content.candidate_snapshot
            if (
                item.source_incident_id == self.scope.incident_id
                or draft.service_name != self.scope.service_name
                or draft.environment != self.scope.environment
                or draft.data_source_mode != self.scope.data_source_mode
                or item.entry.created_at > self.selected_at
            ):
                raise ValueError("经验上下文与系统范围或选择时间不一致")
        return self
