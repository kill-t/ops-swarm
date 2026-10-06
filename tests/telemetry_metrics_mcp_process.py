"""供集成测试启动的真实指标 MCP 进程，不连接 Prometheus 或业务数据库。"""

from __future__ import annotations

from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.observation_window import ObservationWindow
from opsswarm.telemetry_metrics_mcp.server import (
    TelemetryMetricsMcpServerSettings,
    create_server,
)


class FixedMetricSource:
    def query_metrics_in_window(
        self, *, service_name: str, window: ObservationWindow
    ) -> tuple[EvidenceObservation, ...]:
        del service_name
        start, end = window.window_start, window.window_end
        minutes = int((end - start).total_seconds() // 60)
        return (
            EvidenceObservation(
                evidence_id=f"prometheus-order-service-metrics-{int(end.timestamp())}-fixture",
                kind=EvidenceKind.METRIC,
                source="prometheus-fault-lab",
                summary="固定指标证据。",
                observed_at=end,
                attributes={
                    "service_name": "order-service",
                    "lookback_minutes": minutes,
                    "scrape_target_healthy": True,
                    "http_5xx_ratio_before": 0.1,
                    "http_5xx_ratio_after": 0.2,
                    "http_5xx_ratio_min": 0.1,
                    "http_5xx_ratio_max": 0.2,
                    "http_5xx_ratio_sample_count": 2,
                    "database_pool_saturation": 0.8,
                    "database_pool_saturation_max": 0.9,
                    "database_pool_sample_count": 2,
                    "redis_healthy": True,
                    "redis_unavailable_ratio": 0.0,
                    "redis_unavailable_ratio_max": 0.0,
                    "redis_sample_count": 2,
                    "window_start": start.isoformat(),
                    "window_end": end.isoformat(),
                    "observation_semantics": "range_query_aggregation",
                },
            ),
        )


def main() -> None:
    settings = TelemetryMetricsMcpServerSettings(host="127.0.0.1", port=18005)
    create_server(FixedMetricSource()).run(
        transport="streamable-http",
        host=settings.host,
        port=settings.port,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        transport_security=settings.transport_security(),
    )


if __name__ == "__main__":
    main()
