"""仅暴露固定日志查询能力的 Streamable HTTP MCP 服务。"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import UTC
from typing import Annotated, Any, Literal, Protocol, Self, cast

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.shared.exceptions import MCPError
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import AwareDatetime, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.applications import Starlette

from opsswarm.investigation.errors import InvestigationDataSourceFailure
from opsswarm.investigation.loki import LokiOnlyInvestigationDataSource
from opsswarm.investigation.models import EvidenceObservation
from opsswarm.observation_window import ObservationWindow
from opsswarm.telemetry_mcp.contracts import (
    CONTRACT_VERSION,
    MAX_EVIDENCE_ITEMS,
    TOOL_NAME,
    QueryServiceLogsError,
    QueryServiceLogsFailure,
    QueryServiceLogsRequest,
    QueryServiceLogsResponse,
    QueryServiceLogsSuccess,
)

SERVER_NAME = "opsswarm-telemetry-mcp"
SERVER_VERSION = "1.0.0"
DEFAULT_ALLOWED_HOSTS = (
    "127.0.0.1:18004",
    "localhost:18004",
    "telemetry-mcp:8000",
)

logger = logging.getLogger(__name__)


class WindowedLogSource(Protocol):
    def query_logs_in_window(
        self, *, service_name: str, window: ObservationWindow
    ) -> Sequence[EvidenceObservation]:
        """返回固定窗口内的规范化日志证据。"""


class TelemetryMcpServerSettings(BaseSettings):
    """Telemetry MCP 监听范围和 Host 白名单。"""

    model_config = SettingsConfigDict(env_prefix="OPSSWARM_TELEMETRY_MCP_", extra="ignore")

    host: str = Field(default="127.0.0.1", min_length=1)
    port: int = Field(default=18004, ge=1, le=65535)
    allowed_hosts: tuple[str, ...] = DEFAULT_ALLOWED_HOSTS

    @classmethod
    def from_dotenv(cls) -> Self:
        """仅供显式本地命令读取项目根目录的 .env。"""

        constructor = cast(Any, cls)
        return cast(Self, constructor(_env_file=".env", _env_file_encoding="utf-8"))

    def transport_security(self) -> TransportSecuritySettings:
        return TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(self.allowed_hosts),
            allowed_origins=[],
        )


def _result(
    response: QueryServiceLogsSuccess | QueryServiceLogsFailure,
    *,
    is_error: bool,
) -> CallToolResult:
    payload = response.model_dump(mode="json")
    message = (
        f"返回 {len(response.evidence)} 条日志证据"
        if isinstance(response, QueryServiceLogsSuccess)
        else response.error.message
    )
    return CallToolResult(
        content=[TextContent(type="text", text=message)],
        structured_content=payload,
        is_error=is_error,
    )


def _failure(
    request: QueryServiceLogsRequest,
    *,
    code: Literal["invalid_window", "telemetry_source_unavailable", "internal_error"],
    message: str,
    retryable: bool,
) -> CallToolResult:
    return _result(
        QueryServiceLogsFailure(
            service_name=request.service_name,
            window_start=request.window_start,
            window_end=request.window_end,
            error=QueryServiceLogsError(code=code, message=message, retryable=retryable),
        ),
        is_error=True,
    )


def create_server(source: WindowedLogSource | None = None) -> MCPServer[None]:
    """构造单工具 MCP 服务；依赖注入用于测试且不扩大生产能力。"""

    log_source = source if source is not None else LokiOnlyInvestigationDataSource()
    server: MCPServer[None] = MCPServer(
        name=SERVER_NAME,
        title="OpsSwarm Telemetry MCP",
        description="OpsSwarm 本地故障演练日志只读服务",
        version=SERVER_VERSION,
    )

    async def reject_unexpected_arguments(ctx: Any, call_next: Any) -> Any:
        """SDK 默认忽略额外函数参数，这里在协议入口明确拒绝能力扩张。"""

        if ctx.method == "tools/call" and isinstance(ctx.params, dict):
            arguments = ctx.params.get("arguments")
            if isinstance(arguments, dict) and not set(arguments) <= {
                "service_name",
                "window_start",
                "window_end",
            }:
                raise MCPError(code=-32602, message="Invalid request parameters")
        return await call_next(ctx)

    server.middleware.append(reject_unexpected_arguments)

    @server.tool(
        name=TOOL_NAME,
        description="查询 order-service 固定 UTC 窗口内的规范化故障日志证据。",
        annotations=ToolAnnotations(
            read_only_hint=True,
            destructive_hint=False,
            idempotent_hint=True,
            open_world_hint=False,
        ),
        structured_output=True,
    )
    def query_service_logs(
        service_name: Literal["order-service"],
        window_start: Annotated[AwareDatetime, Field(description="UTC 窗口起点")],
        window_end: Annotated[AwareDatetime, Field(description="UTC 窗口终点")],
    ) -> Annotated[CallToolResult, QueryServiceLogsResponse]:
        try:
            request = QueryServiceLogsRequest(
                service_name=service_name,
                window_start=window_start,
                window_end=window_end,
            )
        except ValueError:
            failure = QueryServiceLogsFailure(
                service_name=service_name,
                window_start=window_start.astimezone(UTC),
                window_end=window_end.astimezone(UTC),
                error=QueryServiceLogsError(
                    code="invalid_window",
                    message="观测窗口必须使用 UTC、为正且不超过 60 个完整分钟",
                    retryable=False,
                ),
            )
            return _result(failure, is_error=True)

        try:
            window = ObservationWindow(
                window_start=request.window_start,
                window_end=request.window_end,
            )
            evidence = tuple(
                log_source.query_logs_in_window(
                    service_name=request.service_name,
                    window=window,
                )
            )
            if len(evidence) > MAX_EVIDENCE_ITEMS:
                logger.error("日志来源违反最多三条证据的服务端约束")
                return _failure(
                    request,
                    code="internal_error",
                    message="Telemetry MCP 无法返回有效结果",
                    retryable=False,
                )
            return _result(
                QueryServiceLogsSuccess(
                    service_name=request.service_name,
                    window_start=request.window_start,
                    window_end=request.window_end,
                    evidence=evidence,
                ),
                is_error=False,
            )
        except InvestigationDataSourceFailure as error:
            retryable = error.error_code in {
                "loki_query_timeout",
                "loki_query_unavailable",
                "loki_unexpected_status",
            }
            return _failure(
                request,
                code="telemetry_source_unavailable",
                message="日志数据源暂时不可用",
                retryable=retryable,
            )
        except ValueError:
            return _failure(
                request,
                code="invalid_window",
                message="观测窗口不符合日志数据源约束",
                retryable=False,
            )
        except Exception:
            logger.error("Telemetry MCP 日志查询发生未预期错误")
            return _failure(
                request,
                code="internal_error",
                message="Telemetry MCP 无法返回有效结果",
                retryable=False,
            )

    tool = server._tool_manager.get_tool(TOOL_NAME)
    if tool is None:  # pragma: no cover - SDK 注册失败时服务本身无法构造
        raise RuntimeError("Telemetry MCP 日志工具注册失败")
    # SDK v2.3 的函数参数模型默认忽略额外字段；本服务必须在协议层拒绝查询扩权参数。
    tool.fn_metadata.arg_model.model_config["extra"] = "forbid"
    tool.fn_metadata.arg_model.model_rebuild(force=True)
    tool.parameters = tool.fn_metadata.arg_model.model_json_schema()
    return server


def create_app(
    *,
    settings: TelemetryMcpServerSettings | None = None,
    source: WindowedLogSource | None = None,
) -> Starlette:
    """创建默认无会话、JSON 响应的生产 ASGI 应用。"""

    resolved = settings if settings is not None else TelemetryMcpServerSettings()
    return create_server(source).streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=resolved.transport_security(),
        host=resolved.host,
    )


def main() -> None:
    """从环境配置启动 Streamable HTTP 服务。"""

    settings = TelemetryMcpServerSettings()
    create_server().run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=settings.transport_security(),
    )


__all__ = [
    "CONTRACT_VERSION",
    "SERVER_NAME",
    "SERVER_VERSION",
    "TOOL_NAME",
    "TelemetryMcpServerSettings",
    "create_app",
    "create_server",
    "main",
]
