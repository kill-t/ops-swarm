"""模型预算、应用输入和逐次报告用量的版本化契约。"""

from decimal import Decimal
from typing import Annotated, Any, Literal, Self

from pydantic import ConfigDict, Field, model_validator

from opsswarm.investigation.attempts import ModelPricing
from opsswarm.investigation.tasks.contracts import TaskModel

Money = Annotated[Decimal, Field(ge=0, max_digits=20, decimal_places=6)]


class ModelProfile(TaskModel):
    profile_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.-]+$")
    model_name: str = Field(min_length=1, max_length=200)
    context_window_tokens: int = Field(ge=1024, le=2_000_000, strict=True)
    structured_output_method: Literal["json_schema", "json_mode", "function_calling"]
    pricing: ModelPricing


class ModelBudgetPolicy(TaskModel):
    version: Literal["model-budget-v1"] = "model-budget-v1"
    profile: ModelProfile
    max_input_tokens_per_call: int = Field(default=24000, ge=1, le=262144, strict=True)
    max_output_tokens_per_call: int = Field(default=4096, ge=1, le=32768, strict=True)
    safety_margin_tokens: int = Field(default=1024, ge=128, le=8192, strict=True)
    max_input_tokens: int = Field(default=200000, ge=1, le=10_000_000, strict=True)
    max_output_tokens: int = Field(default=24000, ge=1, le=1_000_000, strict=True)
    max_estimated_cost_usd: Money = Field(default=Decimal("1"), gt=0, le=100)

    @model_validator(mode="after")
    def require_window_capacity(self) -> Self:
        if (
            self.max_input_tokens_per_call
            + self.max_output_tokens_per_call
            + self.safety_margin_tokens
            > self.profile.context_window_tokens
        ):
            raise ValueError("单次输入、输出和安全余量之和超过模型窗口")
        return self


class TextModelMessage(TaskModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=False)
    role: Literal["system", "user"]
    content: str = Field(max_length=262144)


class ModelInput(TaskModel):
    messages: tuple[TextModelMessage, ...] = Field(min_length=1, max_length=8)
    output_schema: dict[str, Any]
    tool_schema: dict[str, Any]
    structured_output_method: Literal["json_schema", "json_mode", "function_calling"]


class ModelAllocation(TaskModel):
    estimator_version: Literal["utf8-json-v1"] = "utf8-json-v1"
    prompt_version: str = Field(min_length=1, max_length=100)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_bytes: int = Field(ge=1, le=262144, strict=True)
    estimated_input_tokens: int = Field(ge=1, le=262400, strict=True)
    output_token_limit: int = Field(ge=1, le=32768, strict=True)
    reserved_cost_usd: Money


class ReportedModelUsage(TaskModel):
    input_tokens: int = Field(ge=0, le=1_000_000_000, strict=True)
    output_tokens: int = Field(ge=0, le=1_000_000_000, strict=True)
    total_tokens: int = Field(ge=0, le=2_000_000_000, strict=True)

    @model_validator(mode="after")
    def require_complete_usage(self) -> Self:
        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("模型报告总用量不等于输入和输出之和")
        return self


class ModelBudgetUsage(TaskModel):
    committed_input_tokens: int
    committed_output_tokens: int
    committed_cost_usd: Money
    reported_input_tokens: int
    reported_output_tokens: int
    reported_cost_usd: Money | None
    unknown_model_calls: int
