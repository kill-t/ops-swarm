"""Prometheus 只读调查数据源与 Agent 指标工具测试。"""

from collections.abc import Callable

import httpx2
import pytest
from pydantic import ValidationError

from opsswarm.investigation.errors import (
    InvestigationDataSourceFailure,
    UnsupportedInvestigationTarget,
)
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.prometheus import (
    MAX_SAMPLES_PER_SERIES,
    PROMETHEUS_METRIC_NAMES,
    PROMETHEUS_METRICS_QUERY,
    PROMETHEUS_UP_QUERY,
    PrometheusInvestigationSettings,
    PrometheusOnlyInvestigationDataSource,
)
from opsswarm.investigation.tools import EvidenceCollector, create_investigation_tools

NOW = 1_788_084_800


def _series(
    metric_name: str,
    *,
    values: list[tuple[float, str]] | None = None,
    service: str = "order-service",
) -> dict[str, object]:
    return {
        "metric": {
            "__name__": metric_name,
            "instance": "order-service:8000",
            "job": "order-service",
            "service": service,
        },
        "values": values or [(NOW - 60, "0"), (NOW, "0")],
    }


def _transport(
    *,
    up_value: str = "1",
    range_series: list[dict[str, object]] | None = None,
    status_code: int = 200,
) -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []
    default_series = [
        _series(
            PROMETHEUS_METRIC_NAMES["http_5xx_ratio"],
            values=[(NOW - 60, "0.01"), (NOW, "0.9")],
        ),
        _series(
            PROMETHEUS_METRIC_NAMES["database_pool_saturation"],
            values=[(NOW - 60, "0.2"), (NOW, "1")],
        ),
        _series(
            PROMETHEUS_METRIC_NAMES["redis_unavailable_ratio"],
            values=[(NOW - 60, "0"), (NOW, "1")],
        ),
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if status_code != 200:
            return httpx2.Response(status_code, json={"status": "error"})
        if request.url.path == "/api/v1/query":
            return httpx2.Response(
                200,
                json={
                    "status": "success",
                    "data": {
                        "resultType": "vector",
                        "result": [
                            {
                                "metric": {
                                    "__name__": "up",
                                    "instance": "order-service:8000",
                                    "job": "order-service",
                                },
                                "value": [NOW, up_value],
                            }
                        ],
                    },
                },
            )
        return httpx2.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "matrix",
                    "result": default_series if range_series is None else range_series,
                },
            },
        )

    return httpx2.MockTransport(handler), requests


def _source(
    transport: httpx2.BaseTransport,
    *,
    clock: Callable[[], float] = lambda: float(NOW),
) -> PrometheusOnlyInvestigationDataSource:
    return PrometheusOnlyInvestigationDataSource(
        PrometheusInvestigationSettings(prometheus_base_url="http://prometheus:9090"),
        transport=transport,
        clock=clock,
    )


def test_metrics_tool_runs_fixed_queries_and_collects_normalized_evidence() -> None:
    transport, requests = _transport()
    collector = EvidenceCollector()
    tools = create_investigation_tools(_source(transport), collector)
    metrics_tool = next(tool for tool in tools if tool.name == "query_service_metrics")

    result = metrics_tool.invoke({"service_name": "order-service", "lookback_minutes": 15})

    assert len(result) == 1
    assert len(collector.evidence_items) == 1
    observation = collector.evidence_items[0]
    assert observation.kind is EvidenceKind.METRIC
    assert observation.source == "prometheus-fault-lab"
    assert observation.evidence_id.startswith("prometheus-order-service-metrics-")
    assert observation.attributes == {
        "service_name": "order-service",
        "lookback_minutes": 15,
        "scrape_target_healthy": True,
        "http_5xx_ratio_before": 0.01,
        "http_5xx_ratio_after": 0.9,
        "http_5xx_ratio_min": 0.01,
        "http_5xx_ratio_max": 0.9,
        "http_5xx_ratio_sample_count": 2,
        "database_pool_saturation": 1.0,
        "database_pool_saturation_max": 1.0,
        "database_pool_sample_count": 2,
        "redis_healthy": False,
        "redis_unavailable_ratio": 1.0,
        "redis_unavailable_ratio_max": 1.0,
        "redis_sample_count": 2,
    }
    assert [request.url.path for request in requests] == [
        "/api/v1/query",
        "/api/v1/query_range",
    ]
    assert requests[0].url.params["query"] == PROMETHEUS_UP_QUERY
    assert requests[1].url.params["query"] == PROMETHEUS_METRICS_QUERY
    assert requests[1].url.params["start"] == str(NOW - 15 * 60)
    assert requests[1].url.params["end"] == str(NOW)
    assert int(requests[1].url.params["step"]) >= 2


def test_maximum_window_keeps_each_series_bounded() -> None:
    timestamps = list(range(NOW - 3600, NOW + 1, 30))
    assert len(timestamps) == MAX_SAMPLES_PER_SERIES
    range_series = [
        _series(metric_name, values=[(timestamp, "0") for timestamp in timestamps])
        for metric_name in PROMETHEUS_METRIC_NAMES.values()
    ]
    transport, requests = _transport(range_series=range_series)

    observations = _source(transport).query_metrics(
        service_name="order-service",
        lookback_minutes=60,
    )

    assert observations[0].attributes["http_5xx_ratio_sample_count"] == 121
    assert requests[1].url.params["step"] == "30"


def test_logs_and_changes_return_empty_instead_of_fabricated_evidence() -> None:
    transport, requests = _transport()
    source = _source(transport)

    assert source.query_logs(service_name="order-service", lookback_minutes=15) == ()
    assert source.list_recent_changes(service_name="order-service", lookback_hours=6) == ()
    assert requests == []


def test_unsupported_service_is_rejected_before_network_call() -> None:
    transport, requests = _transport()

    with pytest.raises(UnsupportedInvestigationTarget):
        _source(transport).query_metrics(service_name="payment-service", lookback_minutes=15)

    assert requests == []


@pytest.mark.parametrize("lookback_minutes", [0, 61, True, 1.5])
def test_direct_metric_query_revalidates_lookback(lookback_minutes: object) -> None:
    transport, requests = _transport()

    with pytest.raises(ValueError):
        _source(transport).query_metrics(
            service_name="order-service",
            lookback_minutes=lookback_minutes,  # type: ignore[arg-type]
        )

    assert requests == []


def test_unhealthy_scrape_target_fails_before_range_query() -> None:
    transport, requests = _transport(up_value="0")

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        _source(transport).query_metrics(service_name="order-service", lookback_minutes=15)

    assert captured.value.error_code == "prometheus_target_unhealthy"
    assert len(requests) == 1


@pytest.mark.parametrize(
    ("range_series", "error_code"),
    [
        ([], "prometheus_incomplete_metrics"),
        (
            [
                _series(PROMETHEUS_METRIC_NAMES["http_5xx_ratio"]),
                _series(PROMETHEUS_METRIC_NAMES["database_pool_saturation"]),
                _series(
                    PROMETHEUS_METRIC_NAMES["redis_unavailable_ratio"],
                    service="payment-service",
                ),
            ],
            "prometheus_invalid_labels",
        ),
        (
            [
                _series(PROMETHEUS_METRIC_NAMES["http_5xx_ratio"]),
                _series(PROMETHEUS_METRIC_NAMES["database_pool_saturation"]),
                _series(
                    PROMETHEUS_METRIC_NAMES["redis_unavailable_ratio"],
                    values=[(NOW, "NaN")],
                ),
            ],
            "prometheus_invalid_value",
        ),
    ],
)
def test_invalid_metric_response_fails_closed(
    range_series: list[dict[str, object]],
    error_code: str,
) -> None:
    transport, _ = _transport(range_series=range_series)

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        _source(transport).query_metrics(service_name="order-service", lookback_minutes=15)

    assert captured.value.error_code == error_code


def test_http_failure_is_not_misreported_as_empty_evidence() -> None:
    transport, _ = _transport(status_code=503)

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        _source(transport).query_metrics(service_name="order-service", lookback_minutes=15)

    assert captured.value.error_code == "prometheus_unexpected_status"
    assert captured.value.http_status == 503


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:19090",
        "http://example.com:9090",
        "http://127.0.0.1:19090/graph",
        "http://user:secret@127.0.0.1:19090",
        "http://127.0.0.1:8000",
        "http://prometheus:8000",
    ],
)
def test_settings_reject_unsafe_prometheus_url(url: str) -> None:
    with pytest.raises(ValidationError):
        PrometheusInvestigationSettings(prometheus_base_url=url)
