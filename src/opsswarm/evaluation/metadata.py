"""评测实验身份、资源用量与可恢复失败分类。"""

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from opsswarm.evaluation.models import StrictEvaluationModel

FIXTURE_SINGLE_AGENT_PROMPT_VERSION = "single-agent-v1"
FIXTURE_MULTI_AGENT_PROMPT_VERSION = "multi-agent-parallel-v1"


class EvaluationKind(StrEnum):
    """区分确定性契约评测和真实模型评测。"""

    FIXTURE_CONTRACT = "fixture_contract"
    REAL_MODEL = "real_model"


class EvaluationExperimentSpec(StrictEvaluationModel):
    """跨多次重复运行保持不变的公平对照配置。"""

    experiment_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    evaluation_kind: EvaluationKind
    model_provider: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    model_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/-]*$",
    )
    model_parameters_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    single_agent_prompt_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    multi_agent_prompt_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )

    @model_validator(mode="after")
    def validate_model_identity(self) -> Self:
        """防止冻结假模型与真实模型身份互相伪装。"""

        model_values = (
            self.model_provider,
            self.model_name,
            self.model_parameters_hash,
        )
        if self.evaluation_kind is EvaluationKind.FIXTURE_CONTRACT:
            if any(value is not None for value in model_values):
                raise ValueError("冻结契约评测不能声明真实模型身份")
        elif any(value is None for value in model_values):
            raise ValueError("真实模型评测必须声明提供方、模型名称和参数哈希")
        return self


class EvaluationFailureCounts(StrictEvaluationModel):
    """一次完成运行中已经恢复或被计分的失败尝试数量。"""

    model_request_error: int = Field(default=0, ge=0)
    tool_error: int = Field(default=0, ge=0)
    timeout: int = Field(default=0, ge=0)
    structured_output_error: int = Field(default=0, ge=0)
    safety_rejection: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        """返回所有稳定分类的失败尝试总数。"""

        return (
            self.model_request_error
            + self.tool_error
            + self.timeout
            + self.structured_output_error
            + self.safety_rejection
        )


class EvaluationModeTelemetry(StrictEvaluationModel):
    """一种 Agent 模式在整轮评测中的模型资源和失败统计。"""

    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: Decimal = Field(
        default=Decimal("0"),
        ge=0,
        max_digits=14,
        decimal_places=6,
    )
    failures: EvaluationFailureCounts = Field(default_factory=EvaluationFailureCounts)

    @model_validator(mode="after")
    def validate_total_tokens(self) -> Self:
        """总 Token 必须可由输入和输出重算。"""

        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens 必须等于 input_tokens 与 output_tokens 之和")
        return self


class EvaluationRunTelemetry(StrictEvaluationModel):
    """同一公平对照中两种 Agent 模式的可比较遥测。"""

    single_agent: EvaluationModeTelemetry
    multi_agent: EvaluationModeTelemetry


def empty_evaluation_telemetry() -> EvaluationRunTelemetry:
    """为不访问真实模型的冻结契约运行生成显式零遥测。"""

    return EvaluationRunTelemetry(
        single_agent=EvaluationModeTelemetry(),
        multi_agent=EvaluationModeTelemetry(),
    )


def fixture_experiment_spec(experiment_key: str) -> EvaluationExperimentSpec:
    """为当前冻结运行器生成不可伪装成真实模型的实验定义。"""

    return EvaluationExperimentSpec(
        experiment_key=experiment_key,
        evaluation_kind=EvaluationKind.FIXTURE_CONTRACT,
        single_agent_prompt_version=FIXTURE_SINGLE_AGENT_PROMPT_VERSION,
        multi_agent_prompt_version=FIXTURE_MULTI_AGENT_PROMPT_VERSION,
    )
