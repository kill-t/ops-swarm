"""把本地 Prometheus 白名单指标转换为规范化调查证据。"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal
from urllib.parse import urlsplit

import httpx2
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.investigation.errors import (
    InvestigationDataSourceFailure,
    UnsupportedInvestigationTarget,
)
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation

DEFAULT_PROMETHEUS_BASE_URL = "http://127.0.0.1:19090"
ALLOWED_PROMETHEUS_HOSTS = frozenset({"127.0.0.1", "localhost", "prometheus"})
SUPPORTED_SERVICE = "order-service"
PROMETHEUS_UP_QUERY = 'up{job="order-service",instance="order-service:8000"}'
PROMETHEUS_METRICS_QUERY = (
    '{__name__=~"opsswarm_fault_lab_(http_5xx_ratio|database_pool_saturation|'
    'redis_unavailable_ratio)",job="order-service",instance="order-service:8000",'
    'service="order-service"}'
)
PROMETHEUS_METRIC_NAMES = MappingProxyType(
    {
        "http_5xx_ratio": "opsswarm_fault_lab_http_5xx_ratio",
        "database_pool_saturation": "opsswarm_fault_lab_database_pool_saturation",
        "redis_unavailable_ratio": "opsswarm_fault_lab_redis_unavailable_ratio",
    }
)
MAX_LOOKBACK_MINUTES = 60
MAX_SAMPLES_PER_SERIES = 121
MAX_FUTURE_CLOCK_SKEW_SECONDS = 5.0


class PrometheusInvestigationSettings(BaseSettings):
    """限制调查适配器只能访问本地故障实验室 Prometheus。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_INVESTIGATION_", extra="ignore")

    prometheus_base_url: str = Field(default=DEFAULT_PROMETHEUS_BASE_URL, min_length=1)
    request_timeout_seconds: float = Field(default=3.0, gt=0, le=10)

    @field_validator("prometheus_base_url")
    @classmethod
    def restrict_prometheus_url(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        if parsed.scheme != "http":
            raise ValueError("Prometheus 调查地址只允许 http")
        if parsed.hostname not in ALLOWED_PROMETHEUS_HOSTS:
            raise ValueError("Prometheus 调查地址只允许 localhost、127.0.0.1 或 prometheus")
        if (
            parsed.hostname in {"localhost", "127.0.0.1"}
            and parsed.port != 19090
            or parsed.hostname == "prometheus"
            and parsed.port != 9090
        ):
            raise ValueError("Prometheus 调查地址端口必须匹配本机 19090 或 Compose 9090")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("Prometheus 调查地址不能包含用户信息")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("Prometheus 调查地址不能包含路径、查询参数或片段")
        return normalized


class _PrometheusInstantSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: dict[str, str]
    value: tuple[float, str]


class _PrometheusVectorData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resultType: Literal["vector"]
    result: list[_PrometheusInstantSample]


class _PrometheusVectorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"]
    data: _PrometheusVectorData


class _PrometheusRangeSeries(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: dict[str, str]
    values: list[tuple[float, str]]


class _PrometheusMatrixData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resultType: Literal["matrix"]
    result: list[_PrometheusRangeSeries]


class _PrometheusMatrixResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"]
    data: _PrometheusMatrixData


class _MetricWindow(BaseModel):
    """完成数值和顺序校验后的单指标窗口。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    first: float
    latest: float
    minimum: float
    maximum: float
    sample_count: int


class PrometheusOnlyInvestigationDataSource:
    """读取真实指标；尚未接入的日志和变更端口明确返回空集合。"""

    def __init__(
        self,
        settings: PrometheusInvestigationSettings | None = None,
        *,
        transport: httpx2.BaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings if settings is not None else PrometheusInvestigationSettings()
        self._transport = transport
        self._clock = clock

    def query_logs(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        """Loki 尚未接入时不伪造日志证据。"""

        self._validate_target(service_name)
        self._validate_lookback(lookback_minutes)
        return ()

    def query_metrics(
        self,
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> Sequence[EvidenceObservation]:
        """执行固定健康查询与固定区间查询，并生成一条聚合指标证据。"""

        self._validate_target(service_name)
        self._validate_lookback(lookback_minutes)
        end = int(self._clock())
        start = end - lookback_minutes * 60
        step = max(2, math.ceil((end - start) / (MAX_SAMPLES_PER_SERIES - 1)))

        with httpx2.Client(
            base_url=self._settings.prometheus_base_url,
            timeout=self._settings.request_timeout_seconds,
            transport=self._transport,
            trust_env=False,
        ) as client:
            up_payload = self._get(
                client,
                path="/api/v1/query",
                params={"query": PROMETHEUS_UP_QUERY, "time": end},
            )
            self._validate_up(up_payload, expected_time=end)
            metrics_payload = self._get(
                client,
                path="/api/v1/query_range",
                params={
                    "query": PROMETHEUS_METRICS_QUERY,
                    "start": start,
                    "end": end,
                    "step": step,
                },
            )

        windows = self._validate_metric_windows(
            metrics_payload,
            start=start,
            end=end,
        )
        attributes = self._build_attributes(
            windows,
            service_name=service_name,
            lookback_minutes=lookback_minutes,
        )
        observed_at = datetime.fromtimestamp(end, UTC)
        evidence_id = self._build_evidence_id(observed_at=observed_at, attributes=attributes)
        return (
            EvidenceObservation(
                evidence_id=evidence_id,
                kind=EvidenceKind.METRIC,
                source="prometheus-fault-lab",
                summary=(
                    f"Prometheus 最近 {lookback_minutes} 分钟指标：5xx 比例最新 "
                    f"{windows['http_5xx_ratio'].latest:.3f}，数据库连接池饱和度最新 "
                    f"{windows['database_pool_saturation'].latest:.3f}，"
                    f"Redis 不可用比例最新 {windows['redis_unavailable_ratio'].latest:.3f}。"
                ),
                observed_at=observed_at,
                attributes=attributes,
            ),
        )

    def list_recent_changes(
        self,
        *,
        service_name: str,
        lookback_hours: int,
    ) -> Sequence[EvidenceObservation]:
        """变更记录适配器尚未接入时不伪造变更证据。"""

        self._validate_target(service_name)
        if isinstance(lookback_hours, bool) or not isinstance(lookback_hours, int):
            raise ValueError("lookback_hours 必须是整数")
        if not 1 <= lookback_hours <= 24:
            raise ValueError("lookback_hours 必须在 1 到 24 之间")
        return ()

    @staticmethod
    def _validate_target(service_name: str) -> None:
        if service_name != SUPPORTED_SERVICE:
            raise UnsupportedInvestigationTarget(service_name)

    @staticmethod
    def _validate_lookback(lookback_minutes: int) -> None:
        if isinstance(lookback_minutes, bool) or not isinstance(lookback_minutes, int):
            raise ValueError("lookback_minutes 必须是整数")
        if not 1 <= lookback_minutes <= MAX_LOOKBACK_MINUTES:
            raise ValueError("lookback_minutes 必须在 1 到 60 之间")

    @staticmethod
    def _get(
        client: httpx2.Client,
        *,
        path: str,
        params: dict[str, str | int],
    ) -> object:
        try:
            response = client.get(path, params=params)
        except httpx2.TimeoutException as error:
            raise InvestigationDataSourceFailure(
                error_code="prometheus_query_timeout",
                message="Prometheus 调查查询超时",
            ) from error
        except httpx2.HTTPError as error:
            raise InvestigationDataSourceFailure(
                error_code="prometheus_query_unavailable",
                message="Prometheus 调查查询不可用",
            ) from error
        if not 200 <= response.status_code < 300:
            raise InvestigationDataSourceFailure(
                error_code="prometheus_unexpected_status",
                message="Prometheus 调查查询返回了非预期状态",
                http_status=response.status_code,
            )
        try:
            return response.json()
        except ValueError as error:
            raise InvestigationDataSourceFailure(
                error_code="prometheus_invalid_response",
                message="Prometheus 调查查询返回了无效 JSON",
                http_status=response.status_code,
            ) from error

    @staticmethod
    def _validate_up(payload: object, *, expected_time: int) -> None:
        try:
            response = _PrometheusVectorResponse.model_validate(payload)
        except (ValueError, TypeError) as error:
            raise InvestigationDataSourceFailure(
                error_code="prometheus_invalid_response",
                message="Prometheus 抓取健康查询响应无效",
            ) from error
        if len(response.data.result) != 1:
            raise InvestigationDataSourceFailure(
                error_code="prometheus_ambiguous_up",
                message="Prometheus 抓取健康查询必须且只能返回一个时间序列",
            )
        sample = response.data.result[0]
        if sample.metric != {
            "__name__": "up",
            "instance": "order-service:8000",
            "job": "order-service",
        }:
            raise InvestigationDataSourceFailure(
                error_code="prometheus_invalid_labels",
                message="Prometheus 抓取健康查询返回了不匹配的标签",
            )
        if not abs(sample.value[0] - expected_time) <= MAX_FUTURE_CLOCK_SKEW_SECONDS:
            raise InvestigationDataSourceFailure(
                error_code="prometheus_invalid_timestamp",
                message="Prometheus 抓取健康查询返回了异常时间",
            )
        try:
            value = float(sample.value[1])
        except ValueError as error:
            raise InvestigationDataSourceFailure(
                error_code="prometheus_invalid_value",
                message="Prometheus 抓取健康值不是有效数值",
            ) from error
        if value != 1:
            raise InvestigationDataSourceFailure(
                error_code="prometheus_target_unhealthy",
                message="Prometheus 当前无法抓取订单服务指标",
            )

    @classmethod
    def _validate_metric_windows(
        cls,
        payload: object,
        *,
        start: int,
        end: int,
    ) -> dict[str, _MetricWindow]:
        try:
            response = _PrometheusMatrixResponse.model_validate(payload)
        except (ValueError, TypeError) as error:
            raise InvestigationDataSourceFailure(
                error_code="prometheus_invalid_response",
                message="Prometheus 指标区间查询响应无效",
            ) from error
        if len(response.data.result) != len(PROMETHEUS_METRIC_NAMES):
            raise InvestigationDataSourceFailure(
                error_code="prometheus_incomplete_metrics",
                message="Prometheus 必须返回完整且唯一的白名单指标",
            )

        windows: dict[str, _MetricWindow] = {}
        signal_by_metric = {metric: signal for signal, metric in PROMETHEUS_METRIC_NAMES.items()}
        for series in response.data.result:
            labels = series.metric
            metric_name = labels.get("__name__")
            if metric_name not in signal_by_metric or labels != {
                "__name__": metric_name,
                "job": "order-service",
                "instance": "order-service:8000",
                "service": SUPPORTED_SERVICE,
            }:
                raise InvestigationDataSourceFailure(
                    error_code="prometheus_invalid_labels",
                    message="Prometheus 指标区间查询返回了不匹配的标签",
                )
            signal = signal_by_metric[metric_name]
            if signal in windows:
                raise InvestigationDataSourceFailure(
                    error_code="prometheus_incomplete_metrics",
                    message="Prometheus 返回了重复的白名单指标",
                )
            windows[signal] = cls._validate_values(series.values, start=start, end=end)

        if set(windows) != set(PROMETHEUS_METRIC_NAMES):
            raise InvestigationDataSourceFailure(
                error_code="prometheus_incomplete_metrics",
                message="Prometheus 缺少白名单指标",
            )
        return windows

    @staticmethod
    def _validate_values(
        values: list[tuple[float, str]],
        *,
        start: int,
        end: int,
    ) -> _MetricWindow:
        if not values or len(values) > MAX_SAMPLES_PER_SERIES:
            raise InvestigationDataSourceFailure(
                error_code="prometheus_invalid_sample_count",
                message="Prometheus 指标样本数量超出允许范围",
            )
        parsed: list[float] = []
        previous_timestamp: float | None = None
        for timestamp, raw_value in values:
            if not start <= timestamp <= end + MAX_FUTURE_CLOCK_SKEW_SECONDS or (
                previous_timestamp is not None and timestamp <= previous_timestamp
            ):
                raise InvestigationDataSourceFailure(
                    error_code="prometheus_invalid_timestamp",
                    message="Prometheus 指标样本时间不在请求窗口内或顺序异常",
                )
            previous_timestamp = timestamp
            try:
                value = float(raw_value)
            except ValueError as error:
                raise InvestigationDataSourceFailure(
                    error_code="prometheus_invalid_value",
                    message="Prometheus 指标样本不是有效数值",
                ) from error
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise InvestigationDataSourceFailure(
                    error_code="prometheus_invalid_value",
                    message="Prometheus 指标样本超出允许范围",
                )
            parsed.append(value)
        return _MetricWindow(
            first=parsed[0],
            latest=parsed[-1],
            minimum=min(parsed),
            maximum=max(parsed),
            sample_count=len(parsed),
        )

    @staticmethod
    def _build_attributes(
        windows: dict[str, _MetricWindow],
        *,
        service_name: str,
        lookback_minutes: int,
    ) -> dict[str, JsonValue]:
        http = windows["http_5xx_ratio"]
        pool = windows["database_pool_saturation"]
        redis = windows["redis_unavailable_ratio"]
        return {
            "service_name": service_name,
            "lookback_minutes": lookback_minutes,
            "scrape_target_healthy": True,
            "http_5xx_ratio_before": http.first,
            "http_5xx_ratio_after": http.latest,
            "http_5xx_ratio_min": http.minimum,
            "http_5xx_ratio_max": http.maximum,
            "http_5xx_ratio_sample_count": http.sample_count,
            "database_pool_saturation": pool.latest,
            "database_pool_saturation_max": pool.maximum,
            "database_pool_sample_count": pool.sample_count,
            "redis_healthy": redis.latest < 0.5,
            "redis_unavailable_ratio": redis.latest,
            "redis_unavailable_ratio_max": redis.maximum,
            "redis_sample_count": redis.sample_count,
        }

    @staticmethod
    def _build_evidence_id(
        *,
        observed_at: datetime,
        attributes: dict[str, JsonValue],
    ) -> str:
        digest = hashlib.sha256(
            json.dumps(
                attributes,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]
        return f"prometheus-order-service-metrics-{int(observed_at.timestamp())}-{digest}"
