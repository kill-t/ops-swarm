from collections.abc import Sequence
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import anyio
import httpx2
import pytest
from mcp.types import CallToolResult, TextContent
from pydantic import ValidationError

from opsswarm.investigation.errors import InvestigationDataSourceFailure
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.observation_window import ObservationWindow
from opsswarm.telemetry_mcp.client import (
    TelemetryMcpClientSettings,
    TelemetryMcpLogDataSource,
)
from opsswarm.telemetry_mcp.contracts import (
    QueryServiceLogsError,
    QueryServiceLogsFailure,
    QueryServiceLogsRequest,
    QueryServiceLogsSuccess,
)
from opsswarm.telemetry_mcp.server import TelemetryMcpServerSettings, create_app, create_server

START = datetime(2026, 10, 4, 1, 0, tzinfo=UTC)
END = START + timedelta(minutes=15)
WINDOW = ObservationWindow(window_start=START, window_end=END)


def _evidence(*, source: str = "loki-fault-lab") -> EvidenceObservation:
    return EvidenceObservation(
        evidence_id="loki-order-service-release-500-1",
        kind=EvidenceKind.LOG,
        source=source,
        summary="order-service 出现未处理异常。",
        observed_at=START + timedelta(minutes=1),
        attributes={
            "service_name": "order-service",
            "window_start": START.isoformat(),
            "window_end": END.isoformat(),
        },
    )


class FixedSource:
    def query_logs_in_window(
        self, *, service_name: str, window: ObservationWindow
    ) -> Sequence[EvidenceObservation]:
        return (_evidence(),)


@pytest.mark.asyncio
async def test_client_and_server_interoperate_over_real_streamable_http_asgi() -> None:
    settings = TelemetryMcpServerSettings(
        host="127.0.0.1",
        port=18004,
        allowed_hosts=("127.0.0.1:18004",),
    )
    app = create_app(settings=settings, source=FixedSource())
    transport = httpx2.ASGITransport(app=app)
    client = TelemetryMcpLogDataSource(transport=transport)

    async with app.router.lifespan_context(app):
        evidence = await client.query_logs_in_window_async(
            service_name="order-service",
            window=WINDOW,
        )

    assert evidence == (_evidence(),)


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:18004/mcp",
        "http://127.0.0.1:18004/other",
        "http://example.com:18004/mcp",
        "http://telemetry-mcp:8000/mcp?url=http://attacker.invalid",
    ],
)
def test_client_settings_reject_non_whitelisted_endpoints(endpoint: str) -> None:
    with pytest.raises(ValidationError, match="本地白名单"):
        TelemetryMcpClientSettings(endpoint_url=endpoint)


def test_client_rejects_mismatched_service_identity() -> None:
    with pytest.raises(InvestigationDataSourceFailure) as captured:
        TelemetryMcpLogDataSource._validate_identity("impostor", "1.0.0")

    assert captured.value.error_code == "telemetry_mcp_protocol_error"


def test_client_rejects_success_with_untrusted_evidence_source() -> None:
    request = QueryServiceLogsRequest(
        service_name="order-service",
        window_start=START,
        window_end=END,
    )
    response = QueryServiceLogsSuccess(
        service_name="order-service",
        window_start=START,
        window_end=END,
        evidence=(_evidence(source="untrusted-loki"),),
    )
    result = CallToolResult(
        content=[TextContent(type="text", text="伪造结果")],
        structured_content=response.model_dump(mode="json"),
        is_error=False,
    )

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        TelemetryMcpLogDataSource._validate_result(result, request)

    assert captured.value.error_code == "telemetry_mcp_invalid_result"


def test_client_rejects_tool_list_expansion() -> None:
    tools = [SimpleNamespace(name="query_service_logs"), SimpleNamespace(name="query_anything")]

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        TelemetryMcpLogDataSource._validate_tools(tools)

    assert captured.value.error_code == "telemetry_mcp_protocol_error"


@pytest.mark.parametrize("schema_name", ["input", "output"])
def test_client_rejects_tool_schema_drift(schema_name: str) -> None:
    tool = anyio.run(create_server(FixedSource()).list_tools)[0]
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
        TelemetryMcpLogDataSource._validate_tools([changed])

    assert captured.value.error_code == "telemetry_mcp_protocol_error"


@pytest.mark.asyncio
async def test_client_maps_grouped_transport_timeout_to_stable_error() -> None:
    def timeout(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("secret endpoint timed out", request=request)

    client = TelemetryMcpLogDataSource(transport=httpx2.MockTransport(timeout))
    with pytest.raises(InvestigationDataSourceFailure) as captured:
        await client.query_logs_in_window_async(
            service_name="order-service",
            window=WINDOW,
        )

    assert captured.value.error_code == "telemetry_mcp_timeout"
    assert "secret" not in str(captured.value)


@pytest.mark.parametrize("invalid", ["missing", "error_flag", "tool_failure"])
def test_client_rejects_malformed_or_failed_tool_result(invalid: str) -> None:
    request = QueryServiceLogsRequest(
        service_name="order-service",
        window_start=START,
        window_end=END,
    )
    success = QueryServiceLogsSuccess(
        service_name="order-service",
        window_start=START,
        window_end=END,
        evidence=(_evidence(),),
    )
    failure = QueryServiceLogsFailure(
        service_name="order-service",
        window_start=START,
        window_end=END,
        error=QueryServiceLogsError(
            code="telemetry_source_unavailable",
            message="日志数据源暂时不可用",
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
        TelemetryMcpLogDataSource._validate_result(result, request)

    assert captured.value.error_code == (
        "telemetry_mcp_tool_error" if invalid == "tool_failure" else "telemetry_mcp_invalid_result"
    )


@pytest.mark.parametrize("invalid", ["duplicate", "outside_window"])
def test_client_rejects_unbounded_evidence(invalid: str) -> None:
    request = QueryServiceLogsRequest(
        service_name="order-service",
        window_start=START,
        window_end=END,
    )
    evidence = _evidence()
    if invalid == "duplicate":
        items = (evidence, evidence)
    else:
        items = (evidence.model_copy(update={"observed_at": END + timedelta(seconds=1)}),)
    response = QueryServiceLogsSuccess(
        service_name="order-service",
        window_start=START,
        window_end=END,
        evidence=items,
    )
    result = CallToolResult(
        content=[TextContent(type="text", text="越界结果")],
        structured_content=response.model_dump(mode="json"),
        is_error=False,
    )

    with pytest.raises(InvestigationDataSourceFailure) as captured:
        TelemetryMcpLogDataSource._validate_result(result, request)

    assert captured.value.error_code == "telemetry_mcp_invalid_result"
