"""真实模型评测尝试、预算与终态模型。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from opsswarm.evaluation.metadata import (
    EvaluationExperimentSpec,
    EvaluationKind,
    EvaluationModeTelemetry,
)
from opsswarm.evaluation.models import (
    InvestigationEvaluationDataset,
    InvestigationModeBenchmark,
    StrictEvaluationModel,
)
from opsswarm.investigation.models import InvestigationMode


class EvaluationAttemptStatus(StrEnum):
    """真实模型调用尝试的显式生命周期。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class EvaluationFailureCategory(StrEnum):
    """不暴露供应商异常细节的稳定失败分类。"""

    MODEL_REQUEST_ERROR = "model_request_error"
    TOOL_ERROR = "tool_error"
    TIMEOUT = "timeout"
    STRUCTURED_OUTPUT_ERROR = "structured_output_error"
    SAFETY_REJECTION = "safety_rejection"
    BUDGET_EXCEEDED = "budget_exceeded"


class EvaluationAttemptBudget(StrictEvaluationModel):
    """每种模式启动前冻结的资源与费用护栏。"""

    timeout_seconds: int = Field(ge=10, le=600)
    max_model_requests: int = Field(ge=1, le=20)
    max_tool_calls: int = Field(ge=1, le=20)
    max_output_tokens_per_request: int = Field(ge=64, le=8192)
    max_estimated_cost_usd: Decimal = Field(gt=0, max_digits=10, decimal_places=6)
    input_cost_per_million_usd: Decimal = Field(ge=0, max_digits=10, decimal_places=6)
    output_cost_per_million_usd: Decimal = Field(ge=0, max_digits=10, decimal_places=6)

    @model_validator(mode="after")
    def validate_output_cost_ceiling(self) -> Self:
        """仅输出 Token 的理论上限也不能超过整次尝试预算。"""

        maximum_output_cost = (
            Decimal(self.max_model_requests)
            * Decimal(self.max_output_tokens_per_request)
            * self.output_cost_per_million_usd
            / Decimal(1_000_000)
        )
        if maximum_output_cost > self.max_estimated_cost_usd:
            raise ValueError("最大输出 Token 的理论费用已超过尝试预算")
        return self


class EvaluationAttemptSubmission(StrictEvaluationModel):
    """在真实模型调用前原子登记的一种模式尝试。"""

    dataset: InvestigationEvaluationDataset
    experiment: EvaluationExperimentSpec
    runner_name: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    repetition_index: int = Field(ge=1, le=1000)
    scenario_id: str = Field(min_length=1, max_length=100)
    mode: InvestigationMode
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    budget: EvaluationAttemptBudget
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def normalize_started_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("评测尝试开始时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_real_model_scenario(self) -> Self:
        """尝试只能绑定真实模型实验和冻结数据集中的现有场景。"""

        if self.experiment.evaluation_kind is not EvaluationKind.REAL_MODEL:
            raise ValueError("评测尝试必须绑定真实模型实验")
        if self.scenario_id not in {item.scenario_id for item in self.dataset.scenarios}:
            raise ValueError("scenario_id 不属于声明的冻结评测集")
        return self


class StoredEvaluationAttempt(StrictEvaluationModel):
    """真实模型尝试的完整只读审计表示。"""

    id: UUID
    sequence: int = Field(ge=1)
    dataset_key: str
    dataset_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment: EvaluationExperimentSpec
    runner_name: str
    repetition_index: int = Field(ge=1, le=1000)
    scenario_id: str
    mode: InvestigationMode
    idempotency_key: str
    status: EvaluationAttemptStatus
    budget: EvaluationAttemptBudget
    telemetry: EvaluationModeTelemetry
    result: InvestigationModeBenchmark | None
    failure_category: EvaluationFailureCategory | None
    failure_message: str | None
    started_at: datetime
    finished_at: datetime | None
    created_at: datetime

    @model_validator(mode="after")
    def validate_terminal_shape(self) -> Self:
        if self.status is EvaluationAttemptStatus.RUNNING:
            if any(
                value is not None
                for value in (
                    self.result,
                    self.failure_category,
                    self.failure_message,
                    self.finished_at,
                )
            ):
                raise ValueError("运行中尝试不能包含终态字段")
        elif self.status is EvaluationAttemptStatus.SUCCEEDED:
            if self.result is None or self.finished_at is None:
                raise ValueError("成功尝试必须包含结果和完成时间")
            if self.failure_category is not None or self.failure_message is not None:
                raise ValueError("成功尝试不能包含失败信息")
        elif (
            self.result is not None
            or self.finished_at is None
            or self.failure_category is None
            or self.failure_message is None
        ):
            raise ValueError("失败尝试必须只包含失败终态字段")
        if self.result is not None and self.result.mode is not self.mode:
            raise ValueError("尝试模式与结果模式不一致")
        return self


class EvaluationAttemptPage(StrictEvaluationModel):
    """按全局序号倒序读取的评测尝试分页。"""

    items: tuple[StoredEvaluationAttempt, ...]
    next_before_sequence: int | None = Field(default=None, ge=1)


@dataclass(frozen=True, slots=True)
class EvaluationAttemptCreation:
    """区分首次登记和相同幂等键重放。"""

    attempt: StoredEvaluationAttempt
    created: bool
