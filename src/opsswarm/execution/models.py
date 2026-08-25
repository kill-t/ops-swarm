"""确定性操作执行的状态与结果契约。"""

import json
from datetime import timedelta
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

EXECUTION_LEASE_DURATION = timedelta(seconds=15)


class ExecutionStatus(StrEnum):
    """一次幂等操作的持久生命周期。"""

    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class StrictExecutionModel(BaseModel):
    """执行领域不可变值对象的共同约束。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class OperationResult(StrictExecutionModel):
    """白名单适配器返回的有限结构化结果。"""

    output: dict[str, JsonValue]

    @model_validator(mode="after")
    def limit_output_size(self) -> Self:
        encoded = json.dumps(
            self.output,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 16 * 1024:
            raise ValueError("执行结果不能超过 16 KiB")
        return self


class OperationFailure(StrictExecutionModel):
    """可安全持久化和展示的执行失败。"""

    error_code: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9_]+$")
    message: str = Field(min_length=1, max_length=500)
