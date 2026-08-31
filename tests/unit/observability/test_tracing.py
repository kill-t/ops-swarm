from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from pydantic import ValidationError

from opsswarm.observability import (
    TracingSettings,
    capture_current_trace_context,
    create_tracing_runtime,
    extract_trace_context,
    mark_span_error,
)


def test_tracing_is_disabled_by_default() -> None:
    runtime = create_tracing_runtime("test-service", instrument_httpx=False)

    assert runtime.enabled is False
    assert capture_current_trace_context() is None


def test_settings_only_allow_local_jaeger_endpoint() -> None:
    assert (
        TracingSettings(traces_endpoint="http://jaeger:4318/v1/traces").traces_endpoint
        == "http://jaeger:4318/v1/traces"
    )

    for endpoint in (
        "https://telemetry.example.com/v1/traces",
        "http://127.0.0.1:14318/v1/metrics",
        "http://user:secret@127.0.0.1:14318/v1/traces",
    ):
        try:
            TracingSettings(traces_endpoint=endpoint)
        except ValidationError:
            continue
        raise AssertionError(f"不安全追踪地址未被拒绝：{endpoint}")


def test_w3c_context_round_trip_keeps_one_trace_without_sensitive_error_message() -> None:
    exporter = InMemorySpanExporter()
    runtime = create_tracing_runtime(
        "test-service",
        settings=TracingSettings(enabled=True),
        span_exporter=exporter,
        instrument_httpx=False,
    )
    tracer = runtime.get_tracer(__name__)

    with tracer.start_as_current_span("api.request") as parent:
        parent_context = parent.get_span_context()
        carrier = capture_current_trace_context()

    assert carrier is not None
    with tracer.start_as_current_span(
        "worker.attempt",
        context=extract_trace_context(carrier),
        record_exception=False,
        set_status_on_exception=False,
    ) as child:
        mark_span_error(child, RuntimeError("不得进入遥测的供应商响应"))

    spans = {span.name: span for span in exporter.get_finished_spans()}
    child_span = spans["worker.attempt"]
    assert child_span.context is not None
    assert child_span.context.trace_id == parent_context.trace_id
    assert child_span.parent is not None
    assert child_span.parent.span_id == parent_context.span_id
    assert child_span.status.status_code is StatusCode.ERROR
    assert child_span.attributes["error.type"] == "RuntimeError"
    assert child_span.events == ()
    runtime.shutdown()
