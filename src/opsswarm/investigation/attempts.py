"""调查 Worker 单次处理尝试的业务模型。"""

from decimal import Decimal
from enum import StrEnum
from typing import Self

from pydantic import Field, model_validator

from opsswarm.investigation.models import StrictModel


class InvestigationAttemptStatus(StrEnum):
    """一次租约处理尝试的生命周期。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    LEASE_EXPIRED = "lease_expired"


class ModelPricing(StrictModel):
    """模型输入与输出的每百万 Token 美元单价快照。"""

    input_cost_per_million_usd: Decimal = Field(
        ge=0,
        max_digits=14,
        decimal_places=6,
    )
    output_cost_per_million_usd: Decimal = Field(
        ge=0,
        max_digits=14,
        decimal_places=6,
    )


class ModelUsageTelemetry(StrictModel):
    """单次 Worker 尝试内的模型请求与 Token 计数。"""

    model_request_count: int = Field(default=0, ge=0)
    usage_complete: bool = False
    input_tokens: int = Field(default=0, ge=0)
    output_tokens: int = Field(default=0, ge=0)
    total_tokens: int = Field(default=0, ge=0)
    estimated_cost_usd: Decimal | None = Field(
        default=None,
        ge=0,
        max_digits=14,
        decimal_places=6,
    )

    @model_validator(mode="after")
    def validate_total_tokens(self) -> Self:
        """总 Token 必须由输入与输出重算。"""

        if self.total_tokens != self.input_tokens + self.output_tokens:
            raise ValueError("total_tokens 必须等于 input_tokens 与 output_tokens 之和")
        if not self.usage_complete and self.estimated_cost_usd is not None:
            raise ValueError("用量不完整时不能声明估算费用")
        return self
