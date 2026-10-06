from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

import anyio
import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from opsswarm.investigation.errors import InvestigationDataSourceFailure
from opsswarm.investigation.loki import (
    LOKI_LOG_QUERY,
    LokiInvestigationSettings,
    LokiOnlyInvestigationDataSource,
)
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.observation_window import ObservationWindow
from opsswarm.telemetry_mcp.contracts import (
    QUERY_SERVICE_LOGS_RESPONSE_ADAPTER,
    QueryServiceLogsFailure,
    QueryServiceLogsResponse,
    QueryServiceLogsSuccess,
)
from opsswarm.telemetry_mcp.server import SERVER_NAME, TOOL_NAME, create_server
from tests.unit.investigation.test_loki import _line

START = datetime(2026, 10, 4, 1, 0, tzinfo=UTC)
END = START + timedelta(minutes=15)


def _evidence(index: int = 1) -> EvidenceObservation:
    return EvidenceObservation(
        evidence_id=f"loki-order-service-release-500-{index}",
        kind=EvidenceKind.LOG,
        source="loki-fault-lab",
        summary="order-service 出现未处理异常。",
        observed_at=START + timedelta(minutes=index),
        attributes={
            "service_name": "order-service",
            "window_start": START.isoformat(),
            "window_end": END.isoformat(),
        },
    )


class RecordingSource:
    def __init__(self, evidence: Sequence[EvidenceObservation] = ()) -> None:
        self.evidence = evidence
        self.calls: list[tuple[str, ObservationWindow]] = []

    def query_logs_in_window(
        self, *, service_name: str, window: ObservationWindow
    ) -> Sequence[EvidenceObservation]:
        self.calls.append((service_name, window))
        return self.evidence


class FailingSource:
    def query_logs_in_window(
        self, *, service_name: str, window: ObservationWindow
    ) -> Sequence[EvidenceObservation]:
        raise InvestigationDataSourceFailure(
            error_code="loki_query_unavailable",
            message="secret response from http://loki:3100/private",
            http_status=503,
        )


def _arguments(**updates: object) -> dict[str, object]:
    result: dict[str, object] = {
        "service_name": "order-service",
        "window_start": START.isoformat(),
        "window_end": END.isoformat(),
    }
    result.update(updates)
    return result


def test_server_exposes_exactly_one_read_only_bounded_tool() -> None:
    server = create_server(RecordingSource())

    tools = anyio.run(server.list_tools)

    assert server.name == SERVER_NAME
    assert [tool.name for tool in tools] == [TOOL_NAME]
    tool = tools[0]
    assert set(tool.input_schema["properties"]) == {
        "service_name",
        "window_start",
        "window_end",
    }
    assert set(tool.input_schema["required"]) == {
        "service_name",
        "window_start",
        "window_end",
    }
    assert tool.input_schema["additionalProperties"] is False
    assert tool.input_schema["properties"]["service_name"]["const"] == "order-service"
    assert tool.output_schema == QueryServiceLogsResponse.model_json_schema()
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is False


def test_server_reuses_windowed_loki_port_and_returns_structured_success() -> None:
    source = RecordingSource((_evidence(),))
    server = create_server(source)

    result = anyio.run(server.call_tool, TOOL_NAME, _arguments())

    assert isinstance(result, CallToolResult)
    assert result.is_error is False
    response = QUERY_SERVICE_LOGS_RESPONSE_ADAPTER.validate_python(result.structured_content)
    assert isinstance(response, QueryServiceLogsSuccess)
    assert response.evidence == (_evidence(),)
    assert source.calls == [
        ("order-service", ObservationWindow(window_start=START, window_end=END))
    ]


def test_server_keeps_fixed_loki_query_and_limits() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        observed = END - timedelta(seconds=1)
        return httpx2.Response(
            200,
            json={
                "status": "success",
                "data": {
                    "resultType": "streams",
                    "result": [
                        {
                            "stream": {"service": "order-service", "source": "fault-lab"},
                            "values": [
                                [
                                    str(int(observed.timestamp()) * 10**9),
                                    _line(
                                        "redis_unavailable",
                                        timestamp=observed.isoformat(),
                                    ),
                                ]
                            ],
                        }
                    ],
                    "stats": {},
                },
            },
        )

    source = LokiOnlyInvestigationDataSource(
        LokiInvestigationSettings(loki_base_url="http://127.0.0.1:13100"),
        transport=httpx2.MockTransport(handler),
        clock=lambda: (END + timedelta(seconds=1)).timestamp(),
    )
    result = anyio.run(create_server(source).call_tool, TOOL_NAME, _arguments())

    assert isinstance(result, CallToolResult) and result.is_error is False
    assert len(requests) == 1
    params = requests[0].url.params
    assert params["query"] == LOKI_LOG_QUERY
    assert params["start"] == str(int(START.timestamp()) * 10**9)
    assert params["end"] == str(int(END.timestamp()) * 10**9 + 1)
    assert params["limit"] == "100" and params["direction"] == "backward"


def test_server_returns_sanitized_structured_source_failure() -> None:
    server = create_server(FailingSource())

    result = anyio.run(server.call_tool, TOOL_NAME, _arguments())

    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    response = QUERY_SERVICE_LOGS_RESPONSE_ADAPTER.validate_python(result.structured_content)
    assert isinstance(response, QueryServiceLogsFailure)
    assert response.error.code == "telemetry_source_unavailable"
    assert response.error.retryable is True
    serialized = result.model_dump_json(by_alias=True)
    assert "http://loki" not in serialized
    assert "secret response" not in serialized
    assert "traceback" not in serialized.lower()


def test_server_returns_structured_failure_for_invalid_window() -> None:
    server = create_server(RecordingSource())

    result = anyio.run(
        server.call_tool,
        TOOL_NAME,
        _arguments(window_end=(START + timedelta(minutes=61)).isoformat()),
    )

    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    response = QUERY_SERVICE_LOGS_RESPONSE_ADAPTER.validate_python(result.structured_content)
    assert isinstance(response, QueryServiceLogsFailure)
    assert response.error.code == "invalid_window"


def test_server_rejects_arbitrary_query_controls_before_source_call() -> None:
    source = RecordingSource()
    server = create_server(source)

    with pytest.raises(ToolError, match="Extra inputs are not permitted"):
        anyio.run(
            server.call_tool,
            TOOL_NAME,
            _arguments(logql='{service=~".+"}', url="http://attacker.invalid", limit=10_000),
        )
    assert source.calls == []


def test_server_rejects_more_than_three_evidence_items() -> None:
    server = create_server(RecordingSource(tuple(_evidence(index) for index in range(1, 5))))

    result = anyio.run(server.call_tool, TOOL_NAME, _arguments())

    assert isinstance(result, CallToolResult)
    assert result.is_error is True
    response = QUERY_SERVICE_LOGS_RESPONSE_ADAPTER.validate_python(result.structured_content)
    assert isinstance(response, QueryServiceLogsFailure)
    assert response.error.code == "internal_error"
