from collections.abc import Sequence
from datetime import timedelta

import anyio
import httpx2
import pytest
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import CallToolResult

from opsswarm.investigation.errors import InvestigationDataSourceFailure
from opsswarm.investigation.models import EvidenceObservation
from opsswarm.investigation.prometheus import (
    PROMETHEUS_METRICS_QUERY,
    PROMETHEUS_UP_QUERY,
    PrometheusInvestigationSettings,
    PrometheusOnlyInvestigationDataSource,
)
from opsswarm.observation_window import ObservationWindow
from opsswarm.telemetry_metrics_mcp.contracts import (
    QUERY_SERVICE_METRICS_RESPONSE_ADAPTER,
    QueryServiceMetricsFailure,
    QueryServiceMetricsResponse,
    QueryServiceMetricsSuccess,
)
from opsswarm.telemetry_metrics_mcp.server import SERVER_NAME, TOOL_NAME, create_server
from tests.unit.telemetry_metrics_mcp.test_contracts import END, START, metric_evidence


class RecordingSource:
    def __init__(self, evidence: Sequence[EvidenceObservation] = ()) -> None:
        self.evidence = evidence
        self.calls: list[tuple[str, ObservationWindow]] = []

    def query_metrics_in_window(
        self, *, service_name: str, window: ObservationWindow
    ) -> Sequence[EvidenceObservation]:
        self.calls.append((service_name, window))
        return self.evidence


class FailingSource:
    def query_metrics_in_window(
        self, *, service_name: str, window: ObservationWindow
    ) -> Sequence[EvidenceObservation]:
        raise InvestigationDataSourceFailure(
            error_code="prometheus_query_unavailable",
            message="secret response from http://prometheus:9090/private",
            http_status=503,
        )


def arguments(**updates: object) -> dict[str, object]:
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
    assert tool.input_schema["additionalProperties"] is False
    assert tool.input_schema["properties"]["service_name"]["const"] == "order-service"
    assert tool.output_schema == QueryServiceMetricsResponse.model_json_schema()
    assert tool.annotations is not None
    assert tool.annotations.read_only_hint is True
    assert tool.annotations.destructive_hint is False
    assert tool.annotations.idempotent_hint is True
    assert tool.annotations.open_world_hint is False


def test_server_reuses_windowed_metric_port_and_returns_structured_success() -> None:
    source = RecordingSource((metric_evidence(),))
    result = anyio.run(create_server(source).call_tool, TOOL_NAME, arguments())

    assert isinstance(result, CallToolResult) and result.is_error is False
    response = QUERY_SERVICE_METRICS_RESPONSE_ADAPTER.validate_python(result.structured_content)
    assert isinstance(response, QueryServiceMetricsSuccess)
    assert response.evidence == (metric_evidence(),)
    assert source.calls == [
        ("order-service", ObservationWindow(window_start=START, window_end=END))
    ]


def test_server_keeps_fixed_prometheus_queries_and_limits() -> None:
    requests: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(request)
        if request.url.path.endswith("/query"):
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
                                "value": [END.timestamp(), "1"],
                            }
                        ],
                    },
                },
            )
        series = []
        for name, value in (
            ("opsswarm_fault_lab_http_5xx_ratio", "0.4"),
            ("opsswarm_fault_lab_database_pool_saturation", "0.8"),
            ("opsswarm_fault_lab_redis_unavailable_ratio", "0.0"),
        ):
            series.append(
                {
                    "metric": {
                        "__name__": name,
                        "job": "order-service",
                        "instance": "order-service:8000",
                        "service": "order-service",
                    },
                    "values": [[START.timestamp(), value], [END.timestamp(), value]],
                }
            )
        return httpx2.Response(
            200,
            json={"status": "success", "data": {"resultType": "matrix", "result": series}},
        )

    source = PrometheusOnlyInvestigationDataSource(
        PrometheusInvestigationSettings(prometheus_base_url="http://127.0.0.1:19090"),
        transport=httpx2.MockTransport(handler),
        clock=lambda: (END + timedelta(seconds=1)).timestamp(),
    )
    result = anyio.run(create_server(source).call_tool, TOOL_NAME, arguments())

    assert isinstance(result, CallToolResult) and result.is_error is False
    assert len(requests) == 2
    assert requests[0].url.params["query"] == PROMETHEUS_UP_QUERY
    assert requests[0].url.params["time"] == str(int(END.timestamp()))
    range_params = requests[1].url.params
    assert range_params["query"] == PROMETHEUS_METRICS_QUERY
    assert range_params["start"] == str(int(START.timestamp()))
    assert range_params["end"] == str(int(END.timestamp()))
    assert range_params["step"] == "8"


def test_server_returns_sanitized_structured_source_failure() -> None:
    result = anyio.run(create_server(FailingSource()).call_tool, TOOL_NAME, arguments())

    assert isinstance(result, CallToolResult) and result.is_error is True
    response = QUERY_SERVICE_METRICS_RESPONSE_ADAPTER.validate_python(result.structured_content)
    assert isinstance(response, QueryServiceMetricsFailure)
    assert response.error.code == "telemetry_source_unavailable"
    assert response.error.retryable is True
    serialized = result.model_dump_json(by_alias=True)
    assert "http://prometheus" not in serialized
    assert "secret response" not in serialized
    assert "traceback" not in serialized.lower()


def test_server_rejects_arbitrary_query_controls_before_source_call() -> None:
    source = RecordingSource()

    with pytest.raises(ToolError, match="Extra inputs are not permitted"):
        anyio.run(
            create_server(source).call_tool,
            TOOL_NAME,
            arguments(promql="up", url="http://attacker.invalid", step=1),
        )
    assert source.calls == []


@pytest.mark.parametrize("evidence", [(), (metric_evidence(), metric_evidence())])
def test_server_rejects_non_singleton_metric_results(
    evidence: tuple[EvidenceObservation, ...],
) -> None:
    result = anyio.run(create_server(RecordingSource(evidence)).call_tool, TOOL_NAME, arguments())

    assert isinstance(result, CallToolResult) and result.is_error is True
    response = QUERY_SERVICE_METRICS_RESPONSE_ADAPTER.validate_python(result.structured_content)
    assert isinstance(response, QueryServiceMetricsFailure)
    assert response.error.code == "internal_error"
