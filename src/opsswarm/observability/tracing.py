"""OpenTelemetry trace 生命周期与 W3C 上下文持久化。"""

from dataclasses import dataclass
from typing import Any

from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import (
    BatchSpanProcessor,
    SimpleSpanProcessor,
    SpanExporter,
)
from opentelemetry.sdk.trace.sampling import ParentBased, TraceIdRatioBased
from opentelemetry.trace import Span, Status, StatusCode, Tracer
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from pydantic import BaseModel, ConfigDict, Field

from opsswarm import __version__
from opsswarm.observability.settings import TracingSettings

_TRACEPARENT_PATTERN = r"^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$"


class TraceContextCarrier(BaseModel):
    """允许跨 PostgreSQL 任务边界保存的最小 W3C Trace Context。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    traceparent: str = Field(pattern=_TRACEPARENT_PATTERN, max_length=55)
    tracestate: str | None = Field(default=None, max_length=512)


@dataclass(slots=True)
class TracingRuntime:
    """拥有一个进程内 TracerProvider 及自动埋点生命周期。"""

    provider: TracerProvider | None = None
    httpx_instrumented: bool = False
    _closed: bool = False

    @property
    def enabled(self) -> bool:
        return self.provider is not None

    def get_tracer(self, instrumentation_name: str) -> Tracer:
        provider = self.provider or trace.get_tracer_provider()
        return provider.get_tracer(instrumentation_name, __version__)

    def instrument_fastapi(self, app: FastAPI) -> None:
        """健康检查不进入 trace，避免空闲轮询淹没真实事故链路。"""

        if self.provider is None:
            return
        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=self.provider,
            excluded_urls="health",
        )

    def shutdown(self) -> None:
        """尽力刷新 span；遥测关闭不参与业务事务。"""

        if self._closed:
            return
        self._closed = True
        if self.httpx_instrumented:
            HTTPXClientInstrumentor().uninstrument()
        if self.provider is not None:
            self.provider.shutdown()


def create_tracing_runtime(
    service_name: str,
    *,
    settings: TracingSettings | None = None,
    span_exporter: SpanExporter | None = None,
    instrument_httpx: bool = True,
) -> TracingRuntime:
    """创建显式 trace 运行时；测试可注入内存 exporter。"""

    selected = settings or TracingSettings()
    if not selected.enabled:
        return TracingRuntime()
    normalized_service_name = service_name.strip()
    if not normalized_service_name or len(normalized_service_name) > 100:
        raise ValueError("service_name 长度必须在 1 到 100 之间")

    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": normalized_service_name,
                "service.version": __version__,
                "deployment.environment.name": "local-fault-lab",
            }
        ),
        sampler=ParentBased(TraceIdRatioBased(selected.sample_ratio)),
    )
    if span_exporter is None:
        exporter = OTLPSpanExporter(
            endpoint=selected.traces_endpoint,
            timeout=selected.export_timeout_seconds,
        )
        provider.add_span_processor(BatchSpanProcessor(exporter))
    else:
        provider.add_span_processor(SimpleSpanProcessor(span_exporter))

    httpx_instrumented = False
    if instrument_httpx:
        instrumentor = HTTPXClientInstrumentor()
        if not instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.instrument(tracer_provider=provider)
            httpx_instrumented = True
    return TracingRuntime(provider=provider, httpx_instrumented=httpx_instrumented)


def capture_current_trace_context() -> TraceContextCarrier | None:
    """只保存 W3C traceparent/tracestate，不传播 baggage 或用户输入。"""

    current_span = trace.get_current_span()
    if not current_span.get_span_context().is_valid:
        return None
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    traceparent = carrier.get("traceparent")
    if traceparent is None:
        return None
    return TraceContextCarrier(
        traceparent=traceparent,
        tracestate=carrier.get("tracestate"),
    )


def current_trace_id_hex() -> str | None:
    """返回当前有效 trace ID，供 HTTP 响应关联本地 Jaeger。"""

    span_context = trace.get_current_span().get_span_context()
    if not span_context.is_valid:
        return None
    return f"{span_context.trace_id:032x}"


def extract_trace_context(carrier: TraceContextCarrier | None) -> Context | None:
    """把任务携带的远端父上下文还原给 Worker；空值创建新的根 trace。"""

    if carrier is None:
        return None
    raw_carrier: dict[str, str] = carrier.model_dump(exclude_none=True)
    return TraceContextTextMapPropagator().extract(carrier=raw_carrier)


def mark_span_error(span: Span, error: BaseException) -> None:
    """只记录稳定异常类型，不把可能含密钥或响应体的消息写入遥测。"""

    span.set_attribute("error.type", type(error).__name__[:100])
    span.set_status(Status(StatusCode.ERROR))


def span_attributes(**values: Any) -> dict[str, str | int | float | bool]:
    """过滤空值并把 UUID、枚举等对象收敛为安全标量。"""

    attributes: dict[str, str | int | float | bool] = {}
    for key, value in values.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            attributes[key] = value
        else:
            attributes[key] = str(value)
    return attributes
