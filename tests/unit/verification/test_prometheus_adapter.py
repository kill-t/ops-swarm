"""固定 Prometheus 恢复验证适配器测试。"""

from datetime import UTC, datetime
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from opsswarm.investigation.models import HypothesisStatus, Recommendation, RootCause
from opsswarm.investigation.scenarios import (
    DATABASE_POOL_SCENARIO_V2,
    REDIS_UNAVAILABLE_SCENARIO_V2,
    RELEASE_500_SCENARIO,
)
from opsswarm.planning import propose_action_plan
from opsswarm.planning.persistence import StoredActionStep
from opsswarm.verification import (
    PrometheusVerificationAdapter,
    PrometheusVerificationSettings,
    VerificationObservation,
    VerificationProbeFailure,
)
from opsswarm.verification.prometheus_adapter import PROMETHEUS_QUERIES

NOW = 1_788_048_000.0


def _step(root_cause: RootCause, recommendation: Recommendation) -> StoredActionStep:
    scenario = {
        RootCause.RELEASE_500_REGRESSION: RELEASE_500_SCENARIO,
        RootCause.DATABASE_POOL_MISCONFIGURATION: DATABASE_POOL_SCENARIO_V2,
        RootCause.REDIS_UNAVAILABLE: REDIS_UNAVAILABLE_SCENARIO_V2,
    }[root_cause]
    plan = propose_action_plan(
        service_name="order-service",
        root_cause=root_cause,
        recommendation=recommendation,
        hypothesis_status=HypothesisStatus.SUPPORTED,
        evidence_items=scenario.evidence,
    )
    return StoredActionStep(
        id=UUID(int=len(root_cause.value)),
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        **plan.steps[0].model_dump(),
    )


ROLLBACK_STEP = _step(
    RootCause.RELEASE_500_REGRESSION,
    Recommendation.ROLLBACK_RELEASE,
)
CONFIGURATION_STEP = _step(
    RootCause.DATABASE_POOL_MISCONFIGURATION,
    Recommendation.ROLLBACK_CONFIGURATION,
)
REDIS_STEP = _step(
    RootCause.REDIS_UNAVAILABLE,
    Recommendation.RESTORE_DEPENDENCY,
)


def _prometheus_app(
    *,
    metric_name: str,
    value: str = "0",
    observed_at: float = NOW,
    result_count: int = 1,
    service: str = "order-service",
    status_code: int = 200,
) -> tuple[FastAPI, list[str]]:
    app = FastAPI()
    queries: list[str] = []

    @app.get("/api/v1/query")
    async def query(query: str = Query()) -> JSONResponse:
        queries.append(query)
        if status_code != 200:
            return JSONResponse(status_code=status_code, content={"status": "error"})
        sample = {
            "metric": {"__name__": metric_name, "service": service},
            "value": [observed_at, value],
        }
        return JSONResponse(
            content={
                "status": "success",
                "data": {
                    "resultType": "vector",
                    "result": [sample for _ in range(result_count)],
                },
            }
        )

    return app, queries


@pytest.mark.parametrize(
    ("step", "signal", "metric_name", "value"),
    [
        (
            ROLLBACK_STEP,
            "http_5xx_ratio",
            "opsswarm_fault_lab_http_5xx_ratio",
            "0",
        ),
        (
            CONFIGURATION_STEP,
            "database_pool_saturation",
            "opsswarm_fault_lab_database_pool_saturation",
            "0.2",
        ),
        (
            REDIS_STEP,
            "redis_unavailable_ratio",
            "opsswarm_fault_lab_redis_unavailable_ratio",
            "1",
        ),
    ],
)
@pytest.mark.asyncio
async def test_adapter_uses_fixed_query_and_validates_sample(
    step: StoredActionStep,
    signal: str,
    metric_name: str,
    value: str,
) -> None:
    app, queries = _prometheus_app(metric_name=metric_name, value=value)
    adapter = PrometheusVerificationAdapter(
        PrometheusVerificationSettings(prometheus_base_url="http://prometheus:9090"),
        transport=httpx2.ASGITransport(app=app),
        clock=lambda: NOW,
    )

    observation = await adapter.observe(step)

    assert observation == VerificationObservation(
        signal=signal,
        value=float(value),
        http_status=200,
    )
    assert queries == [PROMETHEUS_QUERIES[signal]]


@pytest.mark.asyncio
async def test_adapter_rejects_missing_or_ambiguous_series() -> None:
    missing_app, _ = _prometheus_app(
        metric_name="opsswarm_fault_lab_http_5xx_ratio",
        result_count=0,
    )
    duplicate_app, _ = _prometheus_app(
        metric_name="opsswarm_fault_lab_http_5xx_ratio",
        result_count=2,
    )
    settings = PrometheusVerificationSettings(prometheus_base_url="http://prometheus:9090")

    with pytest.raises(VerificationProbeFailure) as missing:
        await PrometheusVerificationAdapter(
            settings,
            transport=httpx2.ASGITransport(app=missing_app),
            clock=lambda: NOW,
        ).observe(ROLLBACK_STEP)
    with pytest.raises(VerificationProbeFailure) as duplicate:
        await PrometheusVerificationAdapter(
            settings,
            transport=httpx2.ASGITransport(app=duplicate_app),
            clock=lambda: NOW,
        ).observe(ROLLBACK_STEP)

    assert missing.value.error_code == "verification_probe_ambiguous_result"
    assert duplicate.value.error_code == "verification_probe_ambiguous_result"


@pytest.mark.parametrize(
    ("observed_at", "value", "service", "error_code"),
    [
        (NOW - 11, "0", "order-service", "verification_probe_stale_sample"),
        (NOW + 6, "0", "order-service", "verification_probe_stale_sample"),
        (NOW, "broken", "order-service", "verification_probe_invalid_value"),
        (NOW, "NaN", "order-service", "verification_probe_invalid_value"),
        (NOW, "1.1", "order-service", "verification_probe_invalid_value"),
        (NOW, "0", "payment-service", "verification_probe_invalid_labels"),
    ],
)
@pytest.mark.asyncio
async def test_adapter_rejects_untrusted_sample(
    observed_at: float,
    value: str,
    service: str,
    error_code: str,
) -> None:
    app, _ = _prometheus_app(
        metric_name="opsswarm_fault_lab_redis_unavailable_ratio",
        observed_at=observed_at,
        value=value,
        service=service,
    )
    adapter = PrometheusVerificationAdapter(
        PrometheusVerificationSettings(prometheus_base_url="http://prometheus:9090"),
        transport=httpx2.ASGITransport(app=app),
        clock=lambda: NOW,
    )

    with pytest.raises(VerificationProbeFailure) as captured:
        await adapter.observe(REDIS_STEP)

    assert captured.value.error_code == error_code


@pytest.mark.asyncio
async def test_adapter_rejects_prometheus_http_failure() -> None:
    app, _ = _prometheus_app(
        metric_name="opsswarm_fault_lab_http_5xx_ratio",
        status_code=503,
    )
    adapter = PrometheusVerificationAdapter(
        PrometheusVerificationSettings(prometheus_base_url="http://prometheus:9090"),
        transport=httpx2.ASGITransport(app=app),
        clock=lambda: NOW,
    )

    with pytest.raises(VerificationProbeFailure) as captured:
        await adapter.observe(ROLLBACK_STEP)

    assert captured.value.error_code == "verification_probe_unexpected_status"
    assert captured.value.http_status == 503


@pytest.mark.asyncio
async def test_adapter_rejects_step_outside_whitelist_before_query() -> None:
    app, queries = _prometheus_app(metric_name="opsswarm_fault_lab_http_5xx_ratio")
    adapter = PrometheusVerificationAdapter(
        PrometheusVerificationSettings(prometheus_base_url="http://prometheus:9090"),
        transport=httpx2.ASGITransport(app=app),
        clock=lambda: NOW,
    )
    unsafe_step = ROLLBACK_STEP.model_copy(update={"target_name": "payment-service"})

    with pytest.raises(VerificationProbeFailure) as captured:
        await adapter.observe(unsafe_step)

    assert captured.value.error_code == "verification_not_whitelisted"
    assert queries == []


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:19090",
        "http://example.com:9090",
        "http://127.0.0.1:19090/graph",
        "http://user:secret@127.0.0.1:19090",
    ],
)
def test_prometheus_settings_rejects_unsafe_url(url: str) -> None:
    with pytest.raises(ValidationError):
        PrometheusVerificationSettings(prometheus_base_url=url)
