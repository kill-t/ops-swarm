"""通过固定 PromQL 查询读取本地 Prometheus 恢复信号。"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from types import MappingProxyType
from typing import Literal

import httpx2
from pydantic import BaseModel, ConfigDict

from opsswarm.planning import (
    ActionType,
    ConfigurationKey,
    DependencyName,
    RestoreDependencyParameters,
    RollbackConfigurationParameters,
    RollbackReleaseParameters,
    TargetType,
)
from opsswarm.planning.persistence import StoredActionStep
from opsswarm.verification.errors import VerificationProbeFailure
from opsswarm.verification.models import VerificationObservation
from opsswarm.verification.settings import PrometheusVerificationSettings

PROMETHEUS_QUERIES = MappingProxyType(
    {
        "http_5xx_ratio": ('opsswarm_fault_lab_http_5xx_ratio{service="order-service"}'),
        "database_pool_saturation": (
            'opsswarm_fault_lab_database_pool_saturation{service="order-service"}'
        ),
        "redis_unavailable_ratio": (
            'opsswarm_fault_lab_redis_unavailable_ratio{service="order-service"}'
        ),
    }
)
PROMETHEUS_METRIC_NAMES = MappingProxyType(
    {
        "http_5xx_ratio": "opsswarm_fault_lab_http_5xx_ratio",
        "database_pool_saturation": "opsswarm_fault_lab_database_pool_saturation",
        "redis_unavailable_ratio": "opsswarm_fault_lab_redis_unavailable_ratio",
    }
)
MAX_FUTURE_CLOCK_SKEW_SECONDS = 5.0


class _PrometheusSample(BaseModel):
    model_config = ConfigDict(extra="forbid")

    metric: dict[str, str]
    value: tuple[float, str]


class _PrometheusVectorData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    resultType: Literal["vector"]
    result: list[_PrometheusSample]


class _PrometheusQueryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["success"]
    data: _PrometheusVectorData


class PrometheusVerificationAdapter:
    """把三种已冻结规则映射为三条代码内固定查询。"""

    def __init__(
        self,
        settings: PrometheusVerificationSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._settings = settings if settings is not None else PrometheusVerificationSettings()
        self._transport = transport
        self._clock = clock

    async def observe(self, step: StoredActionStep) -> VerificationObservation:
        """校验步骤白名单后执行固定查询，不接受外部 PromQL。"""

        signal = _signal_for_step(step)
        query = PROMETHEUS_QUERIES[signal]
        try:
            async with httpx2.AsyncClient(
                base_url=self._settings.prometheus_base_url,
                timeout=self._settings.request_timeout_seconds,
                transport=self._transport,
                trust_env=False,
            ) as client:
                response = await client.get("/api/v1/query", params={"query": query})
        except httpx2.TimeoutException as error:
            raise VerificationProbeFailure(
                error_code="verification_probe_timeout",
                message="Prometheus 恢复验证查询超时",
            ) from error
        except httpx2.HTTPError as error:
            raise VerificationProbeFailure(
                error_code="verification_probe_unavailable",
                message="Prometheus 恢复验证查询不可用",
            ) from error

        if not 200 <= response.status_code < 300:
            raise VerificationProbeFailure(
                error_code="verification_probe_unexpected_status",
                message="Prometheus 恢复验证返回了非预期状态",
                http_status=response.status_code,
            )
        try:
            payload = _PrometheusQueryResponse.model_validate(response.json())
        except (ValueError, TypeError) as error:
            raise VerificationProbeFailure(
                error_code="verification_probe_invalid_response",
                message="Prometheus 恢复验证返回了无效响应",
                http_status=response.status_code,
            ) from error

        if len(payload.data.result) != 1:
            raise VerificationProbeFailure(
                error_code="verification_probe_ambiguous_result",
                message="Prometheus 恢复验证必须且只能返回一个时间序列",
                http_status=response.status_code,
            )
        sample = payload.data.result[0]
        if (
            sample.metric.get("__name__") != PROMETHEUS_METRIC_NAMES[signal]
            or sample.metric.get("service") != "order-service"
        ):
            raise VerificationProbeFailure(
                error_code="verification_probe_invalid_labels",
                message="Prometheus 恢复验证返回了不匹配的指标标签",
                http_status=response.status_code,
            )

        observed_at, raw_value = sample.value
        age_seconds = self._clock() - observed_at
        if (
            not -MAX_FUTURE_CLOCK_SKEW_SECONDS
            <= age_seconds
            <= (self._settings.max_sample_age_seconds)
        ):
            raise VerificationProbeFailure(
                error_code="verification_probe_stale_sample",
                message="Prometheus 恢复验证样本已过期或时间异常",
                http_status=response.status_code,
            )
        try:
            value = float(raw_value)
        except ValueError as error:
            raise VerificationProbeFailure(
                error_code="verification_probe_invalid_value",
                message="Prometheus 恢复验证指标不是有效数值",
                http_status=response.status_code,
            ) from error
        if not math.isfinite(value) or not 0 <= value <= 1:
            raise VerificationProbeFailure(
                error_code="verification_probe_invalid_value",
                message="Prometheus 恢复验证指标超出允许范围",
                http_status=response.status_code,
            )
        return VerificationObservation(
            signal=signal,
            value=value,
            http_status=response.status_code,
        )


def _signal_for_step(step: StoredActionStep) -> str:
    """动作、参数、目标和信号必须整体匹配固定查询。"""

    if (
        step.action_type is ActionType.ROLLBACK_RELEASE
        and isinstance(step.parameters, RollbackReleaseParameters)
        and step.target_type is TargetType.SERVICE
        and step.target_name == "order-service"
        and step.verification.signal == "http_5xx_ratio"
    ):
        return "http_5xx_ratio"
    if (
        step.action_type is ActionType.ROLLBACK_CONFIGURATION
        and isinstance(step.parameters, RollbackConfigurationParameters)
        and step.parameters.key is ConfigurationKey.DATABASE_POOL_SIZE
        and step.target_type is TargetType.SERVICE
        and step.target_name == "order-service"
        and step.verification.signal == "database_pool_saturation"
    ):
        return "database_pool_saturation"
    if (
        step.action_type is ActionType.RESTORE_DEPENDENCY
        and isinstance(step.parameters, RestoreDependencyParameters)
        and step.parameters.dependency is DependencyName.REDIS
        and step.target_type is TargetType.DEPENDENCY
        and step.target_name == DependencyName.REDIS.value
        and step.verification.signal == "redis_unavailable_ratio"
    ):
        return "redis_unavailable_ratio"
    raise VerificationProbeFailure(
        error_code="verification_not_whitelisted",
        message="Prometheus 恢复验证不支持该动作、参数、目标或信号组合",
    )
