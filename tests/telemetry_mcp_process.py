"""供集成测试启动的真实 MCP 进程，不连接 Loki 或业务数据库。"""

from __future__ import annotations

from datetime import timedelta

from opsswarm.investigation.models import EvidenceObservation
from opsswarm.observation_window import ObservationWindow
from opsswarm.telemetry_mcp.server import TelemetryMcpServerSettings, create_server


class FixedLogSource:
    def query_logs_in_window(
        self, *, service_name: str, window: ObservationWindow
    ) -> tuple[EvidenceObservation, ...]:
        observed_at = window.window_end - timedelta(seconds=1)
        return (
            EvidenceObservation(
                evidence_id=f"mcp-log-{int(observed_at.timestamp())}",
                kind="log",
                source="loki-fault-lab",
                summary="真实 MCP 进程返回一条受限日志证据。",
                observed_at=observed_at,
                attributes={
                    "service_name": service_name,
                    "window_start": window.window_start.isoformat(),
                    "window_end": window.window_end.isoformat(),
                    "lookback_minutes": window.telemetry_minutes(),
                    "event_count": 1,
                },
            ),
        )


if __name__ == "__main__":
    settings = TelemetryMcpServerSettings(host="127.0.0.1", port=18004)
    create_server(FixedLogSource()).run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=settings.transport_security(),
    )
