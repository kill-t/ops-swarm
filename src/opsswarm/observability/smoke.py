"""向本地 Jaeger 发送一条不含业务数据的追踪冒烟。"""

from opentelemetry.trace import Status, StatusCode

from opsswarm.observability import TracingSettings, create_tracing_runtime


def main() -> None:
    """显式开启本地 exporter，验证 OTLP/HTTP 到 Jaeger 的链路。"""

    runtime = create_tracing_runtime(
        "opsswarm-observability-smoke",
        settings=TracingSettings(enabled=True),
        instrument_httpx=False,
    )
    tracer = runtime.get_tracer(__name__)
    try:
        with tracer.start_as_current_span("opsswarm.observability.smoke") as span:
            span.set_attribute("opsswarm.smoke.kind", "local-jaeger")
            span.set_status(Status(StatusCode.OK))
    finally:
        runtime.shutdown()
    print("已向本地 Jaeger 发送 OpenTelemetry 追踪冒烟")


if __name__ == "__main__":
    main()
