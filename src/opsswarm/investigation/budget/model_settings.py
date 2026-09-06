"""Worker 从显式配置构造可核对的模型预算配置快照。"""

from typing import Any, Self, cast

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.investigation.attempts import ModelPricing
from opsswarm.investigation.budget.model_contracts import ModelProfile
from opsswarm.investigation.settings import LlmSettings


class ModelBudgetSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="LLM_", extra="ignore")

    budget_profile_id: str | None = None
    context_window_tokens: int | None = Field(default=None, ge=1024, le=2_000_000)

    @field_validator("budget_profile_id", "context_window_tokens", mode="before")
    @classmethod
    def empty_means_disabled(cls, value: object) -> object:
        return None if isinstance(value, str) and not value.strip() else value

    def to_profile(
        self, settings: LlmSettings, pricing: ModelPricing | None
    ) -> ModelProfile | None:
        if self.budget_profile_id is None or self.context_window_tokens is None or pricing is None:
            return None
        return ModelProfile(
            profile_id=self.budget_profile_id,
            model_name=settings.model,
            context_window_tokens=self.context_window_tokens,
            structured_output_method=settings.structured_output_method,
            pricing=pricing,
        )

    @classmethod
    def from_dotenv(cls) -> Self:
        return cast(Self, cast(Any, cls)(_env_file=".env", _env_file_encoding="utf-8"))
