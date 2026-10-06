"""固定目标、失败关闭的 Telemetry MCP 日志客户端。"""

from __future__ import annotations

from collections.abc import Sequence
from functools import partial
from typing import Any, Self, cast
from urllib.parse import urlsplit

import anyio
import httpx2
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from opsswarm.investigation.errors import InvestigationDataSourceFailure
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.observation_window import ObservationWindow
from opsswarm.telemetry_mcp.contracts import (
    MAX_EVIDENCE_ITEMS,
    QUERY_SERVICE_LOGS_RESPONSE_ADAPTER,
    TOOL_NAME,
    QueryServiceLogsFailure,
    QueryServiceLogsRequest,
    QueryServiceLogsResponse,
    QueryServiceLogsSuccess,
)
from opsswarm.telemetry_mcp.server import SERVER_NAME, SERVER_VERSION

DEFAULT_ENDPOINT_URL = "http://127.0.0.1:18004/mcp"
ALLOWED_ENDPOINTS = frozenset(
    {
        DEFAULT_ENDPOINT_URL,
        "http://localhost:18004/mcp",
        "http://telemetry-mcp:8000/mcp",
    }
)


class TelemetryMcpClientSettings(BaseSettings):
    """客户端只能连接本机或 Compose 中的固定 MCP 端点。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_TELEMETRY_MCP_", extra="ignore")

    endpoint_url: str = DEFAULT_ENDPOINT_URL
    request_timeout_seconds: float = Field(default=3.0, gt=0, le=5)

    @classmethod
    def from_dotenv(cls) -> Self:
        """仅供显式本地命令读取项目根目录的 .env。"""

        constructor = cast(Any, cls)
        return cast(Self, constructor(_env_file=".env", _env_file_encoding="utf-8"))

    @field_validator("endpoint_url")
    @classmethod
    def restrict_endpoint(cls, value: str) -> str:
        normalized = value.rstrip("/")
        parsed = urlsplit(normalized)
        if (
            normalized not in ALLOWED_ENDPOINTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Telemetry MCP 端点不在本地白名单中")
        return normalized


class TelemetryMcpLogDataSource:
    """通过 MCP 读取日志；任何协议偏差都直接失败且不回退旧来源。"""

    def __init__(
        self,
        settings: TelemetryMcpClientSettings | None = None,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings if settings is not None else TelemetryMcpClientSettings()
        self._transport = transport

    def query_logs_in_window(
        self, *, service_name: str, window: ObservationWindow
    ) -> tuple[EvidenceObservation, ...]:
        """同步端口由 Worker 的受限线程调用，网络会话仍使用异步 SDK。"""

        return anyio.run(
            partial(self.query_logs_in_window_async, service_name=service_name, window=window)
        )

    async def query_logs_in_window_async(
        self, *, service_name: str, window: ObservationWindow
    ) -> tuple[EvidenceObservation, ...]:
        """建立一次短连接并完整校验服务身份、工具清单和查询结果。"""

        try:
            request = QueryServiceLogsRequest.model_validate(
                {
                    "service_name": service_name,
                    "window_start": window.window_start,
                    "window_end": window.window_end,
                }
            )
        except ValueError as error:
            raise InvestigationDataSourceFailure(
                error_code="telemetry_mcp_invalid_request",
                message="Telemetry MCP 查询参数无效",
            ) from error

        try:
            timeout = httpx2.Timeout(self._settings.request_timeout_seconds)
            async with (
                httpx2.AsyncClient(
                    timeout=timeout,
                    transport=self._transport,
                    trust_env=False,
                ) as http_client,
                streamable_http_client(
                    self._settings.endpoint_url,
                    http_client=http_client,
                    terminate_on_close=False,
                ) as (read_stream, write_stream),
                ClientSession(
                    read_stream,
                    write_stream,
                    read_timeout_seconds=self._settings.request_timeout_seconds,
                ) as session,
            ):
                initialized = await session.initialize()
                self._validate_identity(
                    initialized.server_info.name,
                    initialized.server_info.version,
                )
                tools = await session.list_tools()
                self._validate_tools(tools.tools)
                result = await session.call_tool(
                    TOOL_NAME,
                    arguments=request.model_dump(mode="json"),
                    read_timeout_seconds=self._settings.request_timeout_seconds,
                )
            return self._validate_result(result, request)
        except InvestigationDataSourceFailure:
            raise
        except Exception as error:
            if _contains_timeout(error):
                raise InvestigationDataSourceFailure(
                    error_code="telemetry_mcp_timeout",
                    message="Telemetry MCP 查询超时",
                ) from error
            raise InvestigationDataSourceFailure(
                error_code="telemetry_mcp_unavailable",
                message="Telemetry MCP 查询不可用",
            ) from error

    @staticmethod
    def _validate_identity(name: str, version: str) -> None:
        if name != SERVER_NAME or version != SERVER_VERSION:
            raise InvestigationDataSourceFailure(
                error_code="telemetry_mcp_protocol_error",
                message="Telemetry MCP 服务身份不匹配",
            )

    @staticmethod
    def _validate_tools(tools: Sequence[object]) -> None:
        if len(tools) != 1:
            raise InvestigationDataSourceFailure(
                error_code="telemetry_mcp_protocol_error",
                message="Telemetry MCP 工具清单不匹配",
            )
        tool = tools[0]
        annotations = getattr(tool, "annotations", None)
        input_schema = getattr(tool, "input_schema", None)
        output_schema = getattr(tool, "output_schema", None)
        if (
            getattr(tool, "name", None) != TOOL_NAME
            or not isinstance(input_schema, dict)
            or not isinstance(output_schema, dict)
            or _contract_schema(input_schema)
            != _contract_schema(QueryServiceLogsRequest.model_json_schema())
            or _contract_schema(output_schema)
            != _contract_schema(QueryServiceLogsResponse.model_json_schema())
            or annotations is None
            or annotations.read_only_hint is not True
            or annotations.destructive_hint is not False
            or annotations.idempotent_hint is not True
            or annotations.open_world_hint is not False
        ):
            raise InvestigationDataSourceFailure(
                error_code="telemetry_mcp_protocol_error",
                message="Telemetry MCP 工具清单不匹配",
            )

    @classmethod
    def _validate_result(
        cls, result: object, request: QueryServiceLogsRequest
    ) -> tuple[EvidenceObservation, ...]:
        try:
            response = QUERY_SERVICE_LOGS_RESPONSE_ADAPTER.validate_python(
                getattr(result, "structured_content", None)
            )
        except (TypeError, ValueError) as error:
            raise InvestigationDataSourceFailure(
                error_code="telemetry_mcp_invalid_result",
                message="Telemetry MCP 返回结构无效",
            ) from error

        is_error = getattr(result, "is_error", None)
        if isinstance(response, QueryServiceLogsFailure):
            if is_error is not True:
                raise InvestigationDataSourceFailure(
                    error_code="telemetry_mcp_invalid_result",
                    message="Telemetry MCP 失败标记不一致",
                )
            raise InvestigationDataSourceFailure(
                error_code="telemetry_mcp_tool_error",
                message="Telemetry MCP 工具执行失败",
            )
        if is_error is not False or not isinstance(response, QueryServiceLogsSuccess):
            raise InvestigationDataSourceFailure(
                error_code="telemetry_mcp_invalid_result",
                message="Telemetry MCP 成功标记不一致",
            )
        cls._validate_scope(response, request)
        return response.evidence

    @staticmethod
    def _validate_scope(
        response: QueryServiceLogsSuccess, request: QueryServiceLogsRequest
    ) -> None:
        if (
            response.service_name != request.service_name
            or response.window_start != request.window_start
            or response.window_end != request.window_end
            or len(response.evidence) > MAX_EVIDENCE_ITEMS
            or len({item.evidence_id for item in response.evidence}) != len(response.evidence)
            or any(
                not request.window_start <= item.observed_at <= request.window_end
                for item in response.evidence
            )
        ):
            raise InvestigationDataSourceFailure(
                error_code="telemetry_mcp_invalid_result",
                message="Telemetry MCP 返回范围与请求不匹配",
            )

        for item in response.evidence:
            attributes = item.attributes
            try:
                evidence_start = QueryServiceLogsRequest.model_validate(
                    {
                        "service_name": request.service_name,
                        "window_start": attributes["window_start"],
                        "window_end": attributes["window_end"],
                    }
                )
            except (KeyError, TypeError, ValueError) as error:
                raise InvestigationDataSourceFailure(
                    error_code="telemetry_mcp_invalid_result",
                    message="Telemetry MCP 证据范围无效",
                ) from error
            if (
                item.kind is not EvidenceKind.LOG
                or item.source != "loki-fault-lab"
                or attributes.get("service_name") != request.service_name
                or evidence_start.window_start != request.window_start
                or evidence_start.window_end != request.window_end
            ):
                raise InvestigationDataSourceFailure(
                    error_code="telemetry_mcp_invalid_result",
                    message="Telemetry MCP 证据来源或范围不匹配",
                )


def _contract_schema(value: object) -> object:
    """忽略说明文本，只比较会改变协议数据形状的 JSON Schema。"""

    if isinstance(value, dict):
        return {
            key: _contract_schema(item)
            for key, item in value.items()
            if key not in {"title", "description"}
        }
    if isinstance(value, list):
        return [_contract_schema(item) for item in value]
    return value


def _contains_timeout(error: BaseException) -> bool:
    """MCP/AnyIO 可能把底层超时包进异常组，审计仍应保留稳定超时码。"""

    if isinstance(error, (TimeoutError, httpx2.TimeoutException)):
        return True
    if isinstance(error, BaseExceptionGroup):
        return any(_contains_timeout(item) for item in error.exceptions)
    return False


__all__ = ["TelemetryMcpClientSettings", "TelemetryMcpLogDataSource"]
