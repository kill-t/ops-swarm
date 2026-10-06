from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.telemetry_metrics_mcp.contracts import (
    QueryServiceMetricsRequest,
    QueryServiceMetricsSuccess,
)

START = datetime(2026, 10, 6, 1, 0, tzinfo=UTC)
END = START + timedelta(minutes=15)


def metric_evidence(**attribute_updates: object) -> EvidenceObservation:
    attributes: dict[str, object] = {
        "service_name": "order-service",
        "lookback_minutes": 15,
        "scrape_target_healthy": True,
        "http_5xx_ratio_before": 0.1,
        "http_5xx_ratio_after": 0.4,
        "http_5xx_ratio_min": 0.1,
        "http_5xx_ratio_max": 0.5,
        "http_5xx_ratio_sample_count": 10,
        "database_pool_saturation": 0.8,
        "database_pool_saturation_max": 0.9,
        "database_pool_sample_count": 10,
        "redis_healthy": True,
        "redis_unavailable_ratio": 0.0,
        "redis_unavailable_ratio_max": 0.1,
        "redis_sample_count": 10,
        "window_start": START.isoformat(),
        "window_end": END.isoformat(),
        "observation_semantics": "range_query_aggregation",
    }
    attributes.update(attribute_updates)
    return EvidenceObservation(
        evidence_id="prometheus-order-service-metrics-1791249300-abcdef123456",
        kind=EvidenceKind.METRIC,
        source="prometheus-fault-lab",
        summary="固定窗口聚合指标。",
        observed_at=END,
        attributes=attributes,
    )


def test_request_accepts_fixed_service_and_utc_window() -> None:
    request = QueryServiceMetricsRequest(
        service_name="order-service", window_start=START, window_end=END
    )

    assert request.window_start.tzinfo is UTC
    assert request.window_end - request.window_start == timedelta(minutes=15)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"service_name": "billing-service"}, "order-service"),
        ({"window_end": START}, "必须为正"),
        ({"window_end": START + timedelta(minutes=61)}, "不超过 60 分钟"),
        ({"window_end": START + timedelta(seconds=90)}, "完整分钟"),
        (
            {
                "window_start": START.astimezone(timezone(timedelta(hours=8))),
                "window_end": END.astimezone(timezone(timedelta(hours=8))),
            },
            "UTC 时区",
        ),
    ],
)
def test_request_rejects_scope_expansion(updates: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "service_name": "order-service",
        "window_start": START,
        "window_end": END,
    }
    values.update(updates)

    with pytest.raises(ValidationError, match=message):
        QueryServiceMetricsRequest.model_validate(values)


def test_request_rejects_arbitrary_query_controls() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        QueryServiceMetricsRequest.model_validate(
            {
                "service_name": "order-service",
                "window_start": START,
                "window_end": END,
                "url": "http://attacker.invalid",
                "promql": "{__name__=~'.+'}",
                "step": 1,
            }
        )


@pytest.mark.parametrize(
    "updates",
    [
        {"lookback_minutes": 60},
        {"scrape_target_healthy": False},
        {"http_5xx_ratio_after": 1.1},
        {"http_5xx_ratio_min": 0.6},
        {"database_pool_sample_count": 122},
        {"database_pool_saturation": 0.9, "database_pool_saturation_max": 0.8},
        {"redis_healthy": False},
        {"observation_semantics": "instant"},
        {"unexpected_metric": 0.2},
    ],
)
def test_success_rejects_invalid_metric_attributes(updates: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        QueryServiceMetricsSuccess(
            service_name="order-service",
            window_start=START,
            window_end=END,
            evidence=(metric_evidence(**updates),),
        )


@pytest.mark.parametrize(
    "change",
    [
        {"kind": EvidenceKind.LOG},
        {"source": "untrusted-prometheus"},
        {"observed_at": END - timedelta(seconds=1)},
        {"evidence_id": "metric-anywhere"},
    ],
)
def test_success_rejects_wrong_metric_scope(change: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        QueryServiceMetricsSuccess(
            service_name="order-service",
            window_start=START,
            window_end=END,
            evidence=(metric_evidence().model_copy(update=change),),
        )


def test_success_requires_exactly_one_metric_evidence() -> None:
    with pytest.raises(ValidationError):
        QueryServiceMetricsSuccess(
            service_name="order-service", window_start=START, window_end=END, evidence=()
        )
