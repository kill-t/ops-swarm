from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import anyio
import httpx2
import pytest
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from opsswarm.investigation.errors import InvestigationDataSourceFailure
from opsswarm.observation_window import ObservationWindow
from opsswarm.telemetry_metrics_mcp.client import (
    TelemetryMcpMetricDataSource,
    TelemetryMetricsMcpClientSettings,
)
from opsswarm.telemetry_metrics_mcp.contracts import (
    QueryServiceMetricsError,
    QueryServiceMetricsFailure,
    QueryServiceMetricsRequest,
    QueryServiceMetricsSuccess,
)
from opsswarm.telemetry_metrics_mcp.server import (
    TelemetryMetricsMcpServerSettings,
    create_app,
    create_server,
)
from tests.unit.telemetry_metrics_mcp.test_contracts import END, START, metric_evidence
from tests.unit.telemetry_metrics_mcp.test_server import RecordingSource

WINDOW = ObservationWindow(window_start=START, window_end=END)


@pytest.mark.asyncio
async def test_client_and_server_interoperate_over_real_streamable_http_asgi() -> None:
    settings = TelemetryMetricsMcpServerSettings(
        host="127.0.0.1", port=18005, allowed_hosts=("127.0.0.1:18005",)
    )
    app = create_app(settings=settings, source=RecordingSource((metric_evidence(),)))
    client = TelemetryMcpMetricDataSource(transport=httpx2.ASGITransport(app=app))

    async with app.router.lifespan_context(app):
        evidence = await client.query_metrics_in_window_async(
            service_name="order-service", window=WINDOW
        )

    assert evidence == (metric_evidence(),)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:18005/mcp",
        "http://127.0.0.1:18005/other",
        "http://example.com:18005/mcp",
        "http://telemetry-metrics-mcp:8000/mcp?url=http://attacker.invalid",
    ],
)
def test_client_settings_reject_non_whitelisted_endpoints(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="本地白名单"):
        TelemetryMetricsMcpClientSettings(endpoint_url=endpoint)


def test_client_rejects_mismatched_service_identity() -> None:
    with pytest.raises(InvestigationDataSourceFailure) as captured:
        TelemetryMcpMetricDataSource._validate_identity("opsswarm-telemetry-mcp", "1.0.0")

    assert captured.value.error_code == "telemetry_mcp_protocol_error"


def test_client_rejects_tool_list_expansion() -> None:
    tools = [SimpleNamespace(name="query_service_metrics"), SimpleNamespace(name="query_anything")]

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        TelemetryMcpMetricDataSource._validate_tools(tools)

    assert captured.value.error_code == "telemetry_mcp_protocol_error"


@pytest.mark.parametrize("schema_name", ["input", "output"])
def test_client_rejects_tool_schema_drift(schema_name: str) -> None:
    tool = anyio.run(create_server(RecordingSource()).list_tools)[0]
    if schema_name == "input":
        schema = deepcopy(tool.input_schema)
        schema["properties"]["service_name"].pop("const")
        changed = tool.model_copy(update={"input_schema": schema})
    else:
        schema = deepcopy(tool.output_schema)
        assert schema is not None
        schema["properties"]["status"] = {"type": "string"}
        changed = tool.model_copy(update={"output_schema": schema})

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        TelemetryMcpMetricDataSource._validate_tools([changed])

    assert captured.value.error_code == "telemetry_mcp_protocol_error"


@pytest.mark.asyncio
async def test_client_maps_grouped_transport_timeout_to_stable_error() -> None:
    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("secret endpoint timed out", request=request)

    client = TelemetryMcpMetricDataSource(transport=httpx2.MockTransport(timeout))
    with pytest.raises(InvestigationDataSourceFailure) as captured:
        await client.query_metrics_in_window_async(service_name="order-service", window=WINDOW)

    assert captured.value.error_code == "telemetry_mcp_timeout"
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize("invalid", ["missing", "error_flag", "tool_failure"])
def test_client_rejects_malformed_or_failed_tool_result(invalid: str) -> None:
    request = QueryServiceMetricsRequest(
        service_name="order-service", window_start=START, window_end=END
    )
    success = QueryServiceMetricsSuccess(
        service_name="order-service",
        window_start=START,
        window_end=END,
        evidence=(metric_evidence(),),
    )
    failure = QueryServiceMetricsFailure(
        service_name="order-service",
        window_start=START,
        window_end=END,
        error=QueryServiceMetricsError(
            code="telemetry_source_unavailable",
            message="指标数据源暂时不可用",
            retryable=True,
        ),
    )
    result = CallToolResult(
        content=[TextContent(type="text", text="固定结果")],
        structured_content=(
            None
            if invalid == "missing"
            else failure.model_dump(mode="json")
            if invalid == "tool_failure"
            else success.model_dump(mode="json")
        ),
        is_error=invalid in {"error_flag", "tool_failure"},
    )

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        TelemetryMcpMetricDataSource._validate_result(result, request)

    assert captured.value.error_code == (
        "telemetry_mcp_tool_error" if invalid == "tool_failure" else "telemetry_mcp_invalid_result"
    )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("source", "untrusted-prometheus"),
        ("observed_at", (END - timedelta(seconds=1)).isoformat()),
        ("http_5xx_ratio_after", 2.0),
        ("http_5xx_ratio_sample_count", 122),
    ],
)
def test_client_rejects_invalid_metric_result(field: str, value: object) -> None:
    request = QueryServiceMetricsRequest(
        service_name="order-service", window_start=START, window_end=END
    )
    payload = QueryServiceMetricsSuccess(
        service_name="order-service",
        window_start=START,
        window_end=END,
        evidence=(metric_evidence(),),
    ).model_dump(mode="json")
    evidence = payload["evidence"][0]
    if field in {"source", "observed_at"}:
        evidence[field] = value
    else:
        evidence["attributes"][field] = value
    result = CallToolResult(
        content=[TextContent(type="text", text="越界结果")],
        structured_content=payload,
        is_error=False,
    )

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        TelemetryMcpMetricDataSource._validate_result(result, request)

    assert captured.value.error_code == "telemetry_mcp_invalid_result"


def test_client_rejects_response_scope_different_from_request() -> None:
    request = QueryServiceMetricsRequest(
        service_name="order-service", window_start=START, window_end=END
    )
    response = QueryServiceMetricsSuccess(
        service_name="order-service",
        window_start=START,
        window_end=END,
        evidence=(metric_evidence(),),
    ).model_dump(mode="json")
    response["window_start"] = (START - timedelta(minutes=15)).isoformat()
    result = CallToolResult(
        content=[TextContent(type="text", text="错误范围")],
        structured_content=response,
        is_error=False,
    )

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        TelemetryMcpMetricDataSource._validate_result(result, request)

    assert captured.value.error_code == "telemetry_mcp_invalid_result"
