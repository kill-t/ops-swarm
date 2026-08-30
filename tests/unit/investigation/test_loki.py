"""Loki 只读调查数据源与 Agent 日志工具测试。"""

import json

import httpx2
import pytest
from pydantic import ValidationError

from opsswarm.investigation.errors import (
    InvestigationDataSourceFailure,
    UnsupportedInvestigationTarget,
)
from opsswarm.investigation.loki import (
    LOKI_LOG_QUERY,
    MAX_LOG_ENTRIES,
    LokiInvestigationSettings,
    LokiOnlyInvestigationDataSource,
)
from opsswarm.investigation.models import EvidenceKind
from opsswarm.investigation.tools import EvidenceCollector, create_investigation_tools

NOW = 1_788_084_800
NOW_NS = NOW * 1_000_000_000
TIMESTAMP = "2026-08-30T10:13:20.000Z"


def _line(fault: str, **updates: object) -> str:
    payload: dict[str, object] = {
        "event": "order_request_failed",
        "fault": fault,
        "level": "warning",
        "route": "/orders/{id}",
        "service": "order-service",
        "status_code": 500,
        "timestamp": TIMESTAMP,
    }
    if fault == "release_500":
        payload.update(error_type="RuntimeError", release_version="2026.01.01-rc1")
    elif fault == "database_pool_timeout":
        payload.update(
            error_type="database_pool_timeout",
            database_pool_size=5,
            database_pool_waiters=5,
        )
    else:
        payload.update(
            error_type="redis_connection_unavailable",
            dependency="redis",
        )
    payload.update(updates)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _transport(
    *,
    streams: list[dict[str, object]] | None = None,
    status_code: int = 200,
) -> tuple[httpx2.MockTransport, list[httpx2.Request]]:
    requests: list[httpx2.Request] = []
    default_streams = [
        {
            "stream": {"service": "order-service", "source": "fault-lab"},
            "values": [
                [str(NOW_NS), _line("release_500")],
                [str(NOW_NS), _line("database_pool_timeout")],
                [str(NOW_NS), _line("redis_unavailable")],
            ],
        }
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if status_code != 200:
            return httpx2.Response(status_code, json={"status": "error"})
        return httpx2.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": default_streams if streams is None else streams,
                    "stats": {"summary": {"totalEntriesReturned": 3}},
                },
            },
        )

    return httpx2.MockTransport(handler), requests


def _source(transport: httpx2.BaseTransport) -> LokiOnlyInvestigationDataSource:
    return LokiOnlyInvestigationDataSource(
        LokiInvestigationSettings(loki_base_url="http://loki:3100"),
        transport=transport,
        clock=lambda: float(NOW),
    )


def test_logs_tool_runs_fixed_query_and_collects_three_fault_evidence() -> None:
    transport, requests = _transport()
    collector = EvidenceCollector()
    tools = create_investigation_tools(_source(transport), collector)
    logs_tool = next(tool for tool in tools if tool.name == "query_service_logs")

    result = logs_tool.invoke({"service_name": "order-service", "lookback_minutes": 15})

    assert len(result) == 3
    assert len(collector.evidence_items) == 3
    assert {item.kind for item in collector.evidence_items} == {EvidenceKind.LOG}
    assert {item.source for item in collector.evidence_items} == {"loki-fault-lab"}
    attributes_by_fault = {
        item.attributes["error_type"]: item.attributes for item in collector.evidence_items
    }
    assert attributes_by_fault["RuntimeError"]["release_version"] == "2026.01.01-rc1"
    assert attributes_by_fault["database_pool_timeout"]["database_pool_size"] == 5
    assert attributes_by_fault["redis_connection_unavailable"]["dependency"] == "redis"
    assert len(requests) == 1
    request = requests[0]
    assert request.url.path == "/loki/api/v1/query_range"
    assert request.url.params["query"] == LOKI_LOG_QUERY
    assert request.url.params["start"] == str(NOW_NS - 15 * 60 * 1_000_000_000)
    assert request.url.params["end"] == str(NOW_NS)
    assert request.url.params["limit"] == str(MAX_LOG_ENTRIES)
    assert request.url.params["direction"] == "backward"


def test_empty_loki_result_is_honest_empty_evidence() -> None:
    transport, _ = _transport(streams=[])

    result = _source(transport).query_logs(
        service_name="order-service",
        lookback_minutes=15,
    )

    assert result == ()


def test_unknown_loki_response_field_is_rejected() -> None:
    transport = httpx2.MockTransport(
        lambda request: httpx2.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": [],
                    "stats": {},
                    "unexpected": request.url.path,
                },
            },
        )
    )

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        _source(transport).query_logs(service_name="order-service", lookback_minutes=15)

    assert captured.value.error_code == "loki_invalid_response"


def test_duplicate_loki_entry_is_deduplicated() -> None:
    line = _line("release_500")
    streams = [
        {
            "stream": {"service": "order-service", "source": "fault-lab"},
            "values": [[str(NOW_NS), line], [str(NOW_NS), line]],
        }
    ]
    transport, _ = _transport(streams=streams)

    result = _source(transport).query_logs(
        service_name="order-service",
        lookback_minutes=15,
    )

    assert len(result) == 1
    assert result[0].attributes["event_count"] == 1


def test_metrics_and_changes_return_empty_instead_of_fabricated_evidence() -> None:
    transport, requests = _transport()
    source = _source(transport)

    assert source.query_metrics(service_name="order-service", lookback_minutes=15) == ()
    assert source.list_recent_changes(service_name="order-service", lookback_hours=6) == ()
    assert requests == []


def test_unsupported_service_is_rejected_before_network_call() -> None:
    transport, requests = _transport()

    with pytest.raises(UnsupportedInvestigationTarget):
        _source(transport).query_logs(service_name="payment-service", lookback_minutes=15)

    assert requests == []


@pytest.mark.parametrize("lookback_minutes", [0, 61, True, 1.5])
def test_direct_log_query_revalidates_lookback(lookback_minutes: object) -> None:
    transport, requests = _transport()

    with pytest.raises(ValueError):
        _source(transport).query_logs(
            service_name="order-service",
            lookback_minutes=lookback_minutes,  # type: ignore[arg-type]
        )

    assert requests == []


@pytest.mark.parametrize(
    ("stream", "error_code"),
    [
        (
            {
                "stream": {"service": "payment-service", "source": "fault-lab"},
                "values": [[str(NOW_NS), _line("release_500")]],
            },
            "loki_invalid_labels",
        ),
        (
            {
                "stream": {"service": "order-service", "source": "fault-lab"},
                "values": [[str(NOW_NS), _line("release_500", order_id="private-001")]],
            },
            "loki_invalid_log_entry",
        ),
        (
            {
                "stream": {"service": "order-service", "source": "fault-lab"},
                "values": [[str(NOW_NS - 2_000_000_000), _line("release_500")]],
            },
            "loki_invalid_log_entry",
        ),
        (
            {
                "stream": {"service": "order-service", "source": "fault-lab"},
                "values": [["not-a-timestamp", _line("release_500")]],
            },
            "loki_invalid_timestamp",
        ),
    ],
)
def test_invalid_stream_or_entry_fails_closed(
    stream: dict[str, object],
    error_code: str,
) -> None:
    transport, _ = _transport(streams=[stream])

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        _source(transport).query_logs(service_name="order-service", lookback_minutes=15)

    assert captured.value.error_code == error_code


def test_response_over_entry_limit_fails_closed() -> None:
    stream = {
        "stream": {"service": "order-service", "source": "fault-lab"},
        "values": [[str(NOW_NS), _line("release_500")] for _ in range(MAX_LOG_ENTRIES + 1)],
    }
    transport, _ = _transport(streams=[stream])

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        _source(transport).query_logs(service_name="order-service", lookback_minutes=15)

    assert captured.value.error_code == "loki_log_limit_exceeded"


def test_http_failure_is_not_misreported_as_empty_evidence() -> None:
    transport, _ = _transport(status_code=503)

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        _source(transport).query_logs(service_name="order-service", lookback_minutes=15)

    assert captured.value.error_code == "loki_unexpected_status"
    assert captured.value.http_status == 503


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1:13100",
        "http://example.com:3100",
        "http://127.0.0.1:13100/ready",
        "http://user:secret@127.0.0.1:13100",
        "http://127.0.0.1:3100",
        "http://loki:13100",
    ],
)
def test_settings_reject_unsafe_loki_url(url: str) -> None:
    with pytest.raises(ValidationError):
        LokiInvestigationSettings(loki_base_url=url)
