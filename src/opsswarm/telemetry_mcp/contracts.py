"""Telemetry MCP 工具的版本化输入与输出契约。"""

from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from opsswarm.investigation.models import EvidenceObservation

TOOL_NAME = "query_service_logs"
CONTRACT_VERSION = "opsswarm.telemetry.query-service-logs.v1"
SUPPORTED_SERVICE = "order-service"
MAX_WINDOW = timedelta(minutes=60)
MAX_EVIDENCE_ITEMS = 3


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _normalize_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("观测窗口必须使用 UTC 时区")
    return value.astimezone(UTC)


class QueryServiceLogsRequest(_StrictContract):
    """只允许固定服务和至多六十分钟的 UTC 窗口。"""

    service_name: Literal["order-service"]
    window_start: AwareDatetime
    window_end: AwareDatetime

    @field_validator("window_start", "window_end")
    @classmethod
    def require_utc(cls, value: datetime) -> datetime:
        return _normalize_utc(value)

    @model_validator(mode="after")
    def validate_window(self) -> Self:
        duration = self.window_end - self.window_start
        if not timedelta(0) < duration <= MAX_WINDOW:
            raise ValueError("观测窗口必须为正且不超过 60 分钟")
        if duration.total_seconds() % 60:
            raise ValueError("观测窗口必须使用完整分钟")
        return self


class QueryServiceLogsError(_StrictContract):
    code: Literal["invalid_window", "telemetry_source_unavailable", "internal_error"]
    message: str = Field(min_length=1, max_length=200)
    retryable: bool


class QueryServiceLogsSuccess(_StrictContract):
    contract_version: Literal["opsswarm.telemetry.query-service-logs.v1"] = (
        "opsswarm.telemetry.query-service-logs.v1"
    )
    tool: Literal["query_service_logs"] = "query_service_logs"
    status: Literal["success"] = "success"
    service_name: Literal["order-service"]
    window_start: datetime
    window_end: datetime
    evidence: tuple[EvidenceObservation, ...] = Field(max_length=MAX_EVIDENCE_ITEMS)

    _utc_start = field_validator("window_start")(_normalize_utc)
    _utc_end = field_validator("window_end")(_normalize_utc)


class QueryServiceLogsFailure(_StrictContract):
    contract_version: Literal["opsswarm.telemetry.query-service-logs.v1"] = (
        "opsswarm.telemetry.query-service-logs.v1"
    )
    tool: Literal["query_service_logs"] = "query_service_logs"
    status: Literal["failure"] = "failure"
    service_name: Literal["order-service"]
    window_start: datetime
    window_end: datetime
    error: QueryServiceLogsError

    _utc_start = field_validator("window_start")(_normalize_utc)
    _utc_end = field_validator("window_end")(_normalize_utc)


_QueryServiceLogsResponseValue = Annotated[
    QueryServiceLogsSuccess | QueryServiceLogsFailure,
    Field(discriminator="status"),
]


class QueryServiceLogsResponse(_StrictContract):
    """SDK 要求 outputSchema 顶层为对象；状态校验仍保持互斥。"""

    contract_version: Literal["opsswarm.telemetry.query-service-logs.v1"] = (
        "opsswarm.telemetry.query-service-logs.v1"
    )
    tool: Literal["query_service_logs"] = "query_service_logs"
    status: Literal["success", "failure"]
    service_name: Literal["order-service"]
    window_start: datetime
    window_end: datetime
    evidence: tuple[EvidenceObservation, ...] | None = Field(default=None, max_length=3)
    error: QueryServiceLogsError | None = None

    _utc_start = field_validator("window_start")(_normalize_utc)
    _utc_end = field_validator("window_end")(_normalize_utc)

    @model_validator(mode="after")
    def require_status_payload(self) -> Self:
        if self.status == "success" and self.evidence is not None and self.error is None:
            return self
        if self.status == "failure" and self.evidence is None and self.error is not None:
            return self
        raise ValueError("成功响应只能包含 evidence，失败响应只能包含 error")


QUERY_SERVICE_LOGS_RESPONSE_ADAPTER: TypeAdapter[_QueryServiceLogsResponseValue] = TypeAdapter(
    _QueryServiceLogsResponseValue
)
