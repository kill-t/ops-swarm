"""Telemetry Metrics MCP 工具的版本化输入与输出契约。"""

from __future__ import annotations

import math
from collections.abc import Mapping
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

from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.investigation.prometheus import MAX_SAMPLES_PER_SERIES

TOOL_NAME = "query_service_metrics"
CONTRACT_VERSION = "opsswarm.telemetry.query-service-metrics.v1"
SUPPORTED_SERVICE = "order-service"
MAX_WINDOW = timedelta(minutes=60)
EXPECTED_ATTRIBUTE_KEYS = frozenset(
    {
        "service_name",
        "lookback_minutes",
        "scrape_target_healthy",
        "http_5xx_ratio_before",
        "http_5xx_ratio_after",
        "http_5xx_ratio_min",
        "http_5xx_ratio_max",
        "http_5xx_ratio_sample_count",
        "database_pool_saturation",
        "database_pool_saturation_max",
        "database_pool_sample_count",
        "redis_healthy",
        "redis_unavailable_ratio",
        "redis_unavailable_ratio_max",
        "redis_sample_count",
        "window_start",
        "window_end",
        "observation_semantics",
    }
)


class _StrictContract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


def _normalize_utc(value: datetime) -> datetime:
    if value.utcoffset() != timedelta(0):
        raise ValueError("观测窗口必须使用 UTC 时区")
    return value.astimezone(UTC)


class QueryServiceMetricsRequest(_StrictContract):
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


class QueryServiceMetricsError(_StrictContract):
    code: Literal["invalid_window", "telemetry_source_unavailable", "internal_error"]
    message: str = Field(min_length=1, max_length=200)
    retryable: bool


def _bounded_ratio(attributes: Mapping[str, object], name: str) -> float:
    raw = attributes.get(name)
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f"{name} 必须是数值")
    value = float(raw)
    if not math.isfinite(value) or not 0 <= value <= 1:
        raise ValueError(f"{name} 必须在 0 到 1 之间")
    return value


def _bounded_sample_count(attributes: Mapping[str, object], name: str) -> int:
    value = attributes.get(name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} 必须是整数")
    if not 1 <= value <= MAX_SAMPLES_PER_SERIES:
        raise ValueError(f"{name} 超出允许范围")
    return value


def validate_metric_evidence(
    evidence: tuple[EvidenceObservation, ...],
    *,
    service_name: str,
    window_start: datetime,
    window_end: datetime,
) -> None:
    """在协议边界重新验证规范化指标的范围、类型和聚合不变量。"""

    if len(evidence) != 1:
        raise ValueError("指标成功响应必须且只能包含一条证据")
    item = evidence[0]
    attributes = dict(item.attributes)
    minutes = int((window_end - window_start).total_seconds() // 60)
    if (
        item.kind is not EvidenceKind.METRIC
        or item.source != "prometheus-fault-lab"
        or item.observed_at != window_end
        or not item.evidence_id.startswith("prometheus-order-service-metrics-")
        or set(attributes) != EXPECTED_ATTRIBUTE_KEYS
        or attributes.get("service_name") != service_name
        or attributes.get("lookback_minutes") != minutes
        or attributes.get("scrape_target_healthy") is not True
        or attributes.get("window_start") != window_start.isoformat()
        or attributes.get("window_end") != window_end.isoformat()
        or attributes.get("observation_semantics") != "range_query_aggregation"
    ):
        raise ValueError("指标证据的来源或范围不匹配")

    http_before = _bounded_ratio(attributes, "http_5xx_ratio_before")
    http_after = _bounded_ratio(attributes, "http_5xx_ratio_after")
    http_min = _bounded_ratio(attributes, "http_5xx_ratio_min")
    http_max = _bounded_ratio(attributes, "http_5xx_ratio_max")
    pool = _bounded_ratio(attributes, "database_pool_saturation")
    pool_max = _bounded_ratio(attributes, "database_pool_saturation_max")
    redis = _bounded_ratio(attributes, "redis_unavailable_ratio")
    redis_max = _bounded_ratio(attributes, "redis_unavailable_ratio_max")
    for name in (
        "http_5xx_ratio_sample_count",
        "database_pool_sample_count",
        "redis_sample_count",
    ):
        _bounded_sample_count(attributes, name)
    if (
        not http_min <= http_before <= http_max
        or not http_min <= http_after <= http_max
        or pool > pool_max
        or redis > redis_max
        or not isinstance(attributes.get("redis_healthy"), bool)
        or attributes["redis_healthy"] is not (redis < 0.5)
    ):
        raise ValueError("指标证据的聚合值不一致")


class QueryServiceMetricsSuccess(_StrictContract):
    contract_version: Literal["opsswarm.telemetry.query-service-metrics.v1"] = (
        "opsswarm.telemetry.query-service-metrics.v1"
    )
    tool: Literal["query_service_metrics"] = "query_service_metrics"
    status: Literal["success"] = "success"
    service_name: Literal["order-service"]
    window_start: AwareDatetime
    window_end: AwareDatetime
    evidence: tuple[EvidenceObservation, ...] = Field(min_length=1, max_length=1)

    _utc_start = field_validator("window_start")(_normalize_utc)
    _utc_end = field_validator("window_end")(_normalize_utc)

    @model_validator(mode="after")
    def validate_evidence(self) -> Self:
        validate_metric_evidence(
            self.evidence,
            service_name=self.service_name,
            window_start=self.window_start,
            window_end=self.window_end,
        )
        return self


class QueryServiceMetricsFailure(_StrictContract):
    contract_version: Literal["opsswarm.telemetry.query-service-metrics.v1"] = (
        "opsswarm.telemetry.query-service-metrics.v1"
    )
    tool: Literal["query_service_metrics"] = "query_service_metrics"
    status: Literal["failure"] = "failure"
    service_name: Literal["order-service"]
    window_start: AwareDatetime
    window_end: AwareDatetime
    error: QueryServiceMetricsError

    _utc_start = field_validator("window_start")(_normalize_utc)
    _utc_end = field_validator("window_end")(_normalize_utc)


_QueryServiceMetricsResponseValue = Annotated[
    QueryServiceMetricsSuccess | QueryServiceMetricsFailure,
    Field(discriminator="status"),
]


class QueryServiceMetricsResponse(_StrictContract):
    """SDK 要求 outputSchema 顶层为对象；状态校验仍保持互斥。"""

    contract_version: Literal["opsswarm.telemetry.query-service-metrics.v1"] = (
        "opsswarm.telemetry.query-service-metrics.v1"
    )
    tool: Literal["query_service_metrics"] = "query_service_metrics"
    status: Literal["success", "failure"]
    service_name: Literal["order-service"]
    window_start: AwareDatetime
    window_end: AwareDatetime
    evidence: tuple[EvidenceObservation, ...] | None = Field(
        default=None, min_length=1, max_length=1
    )
    error: QueryServiceMetricsError | None = None

    _utc_start = field_validator("window_start")(_normalize_utc)
    _utc_end = field_validator("window_end")(_normalize_utc)

    @model_validator(mode="after")
    def require_status_payload(self) -> Self:
        if self.status == "success" and self.evidence is not None and self.error is None:
            validate_metric_evidence(
                self.evidence,
                service_name=self.service_name,
                window_start=self.window_start,
                window_end=self.window_end,
            )
            return self
        if self.status == "failure" and self.evidence is None and self.error is not None:
            return self
        raise ValueError("成功响应只能包含 evidence，失败响应只能包含 error")


QUERY_SERVICE_METRICS_RESPONSE_ADAPTER: TypeAdapter[_QueryServiceMetricsResponseValue] = (
    TypeAdapter(_QueryServiceMetricsResponseValue)
)
