"""显式遥测窗口在时钟推进和异常响应下保持边界。"""

from datetime import UTC, datetime, timedelta

import httpx2
import pytest

from opsswarm.investigation.errors import (
    InvestigationDataSourceFailure,
    UnsupportedInvestigationTarget,
)
from opsswarm.investigation.loki import LokiOnlyInvestigationDataSource
from opsswarm.investigation.prometheus import PrometheusOnlyInvestigationDataSource
from opsswarm.observation_window import ObservationWindow
from tests.unit.investigation import test_loki as loki
from tests.unit.investigation import test_prometheus as prom

END = datetime.fromtimestamp(loki.NOW, UTC)
WINDOW = ObservationWindow(window_start=END - timedelta(minutes=15), window_end=END)


@pytest.mark.parametrize("kind", ["log", "metric"])
def test_explicit_window_survives_clock_advance_and_retry(kind):
    transport, requests = loki._transport() if kind == "log" else prom._transport()
    current = loki.NOW + 3600
    base = transport

    def advance_during_query(request):
        nonlocal current
        current += 120
        return base.handle_request(request)

    transport = httpx2.MockTransport(advance_during_query)
    source_type = (
        LokiOnlyInvestigationDataSource if kind == "log" else PrometheusOnlyInvestigationDataSource
    )
    source = source_type(transport=transport, clock=lambda: current)
    query = source.query_logs_in_window if kind == "log" else source.query_metrics_in_window
    first = query(service_name="order-service", window=WINDOW)
    current += 600
    assert query(service_name="order-service", window=WINDOW) == first
    assert all(item.observed_at <= END for item in first)
    assert all(item.attributes["window_end"] == END.isoformat() for item in first)
    for request in requests:
        params = request.url.params
        if kind == "log":
            assert int(params["start"]) == (loki.NOW - 900) * 10**9
            assert int(params["end"]) == loki.NOW * 10**9 + 1
        elif request.url.path.endswith("query_range"):
            assert int(params["start"]) == prom.NOW - 900 and int(params["end"]) == prom.NOW
        else:
            assert int(params["time"]) == prom.NOW


@pytest.mark.parametrize(
    "invalid", ["stream_after", "event_after", "event_before", "stream_before"]
)
def test_loki_requires_both_timestamps_inside_window(invalid):
    stream_ns, event_time = loki.NOW_NS, END
    if invalid == "stream_after":
        stream_ns += 1
    elif invalid == "event_after":
        event_time += timedelta(microseconds=1)
    else:
        stream_ns = int(WINDOW.window_start.timestamp()) * 10**9
        event_time = WINDOW.window_start
        if invalid == "event_before":
            event_time -= timedelta(microseconds=1)
        else:
            stream_ns -= 1
    transport, _ = loki._transport(
        streams=[
            {
                "stream": {"service": "order-service", "source": "fault-lab"},
                "values": [
                    [str(stream_ns), loki._line("release_500", timestamp=event_time.isoformat())]
                ],
            }
        ]
    )
    with pytest.raises(InvestigationDataSourceFailure) as caught:
        loki._source(transport).query_logs_in_window(service_name="order-service", window=WINDOW)
    assert caught.value.error_code == "loki_invalid_timestamp"


@pytest.mark.parametrize("part", ["up", "samples"])
def test_prometheus_rejects_small_timestamp_drift_in_explicit_window(part):
    base, _ = prom._transport()

    def handler(request):
        payload = base.handle_request(request).json()
        if part == "up" and request.url.path.endswith("query"):
            payload["data"]["result"][0]["value"][0] += 1
        if part == "samples" and request.url.path.endswith("query_range"):
            payload["data"]["result"][0]["values"][-1][0] += 1
        return httpx2.Response(200, json=payload)

    with pytest.raises(InvestigationDataSourceFailure) as caught:
        prom._source(httpx2.MockTransport(handler)).query_metrics_in_window(
            service_name="order-service", window=WINDOW
        )
    assert caught.value.error_code == "prometheus_invalid_timestamp"


def test_empty_logs_are_valid_but_missing_metrics_are_not_health():
    transport, _ = loki._transport(streams=[])
    assert (
        loki._source(transport).query_logs_in_window(service_name="order-service", window=WINDOW)
        == ()
    )
    transport, _ = prom._transport(range_series=[])
    with pytest.raises(InvestigationDataSourceFailure) as caught:
        prom._source(transport).query_metrics_in_window(service_name="order-service", window=WINDOW)
    assert caught.value.error_code == "prometheus_incomplete_metrics"


@pytest.mark.parametrize("kind", ["log", "metric"])
@pytest.mark.parametrize("invalid", ["future", "service", "duration"])
def test_rejected_window_never_issues_http(kind, invalid):
    transport, requests = loki._transport() if kind == "log" else prom._transport()
    source = loki._source(transport) if kind == "log" else prom._source(transport)
    query = source.query_logs_in_window if kind == "log" else source.query_metrics_in_window
    window, service = WINDOW, "order-service"
    if invalid == "future":
        window = ObservationWindow(window_start=END, window_end=END + timedelta(minutes=1))
    elif invalid == "service":
        service = "another-service"
    else:
        window = ObservationWindow(window_start=END - timedelta(hours=2), window_end=END)
    with pytest.raises(UnsupportedInvestigationTarget if invalid == "service" else ValueError):
        query(service_name=service, window=window)
    assert requests == []
