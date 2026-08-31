import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from pydantic import ValidationError

from opsswarm.investigation.errors import UnsupportedInvestigationTarget
from opsswarm.investigation.scenarios import (
    RELEASE_500_SCENARIO,
    FrozenScenarioDataSource,
)
from opsswarm.investigation.tools import (
    INVESTIGATION_TOOL_NAMES,
    EvidenceCollector,
    create_investigation_tools,
)
from opsswarm.observability import TracingSettings, create_tracing_runtime


def test_read_only_tools_return_stable_evidence_and_record_calls() -> None:
    collector = EvidenceCollector()
    tools = create_investigation_tools(
        FrozenScenarioDataSource(RELEASE_500_SCENARIO),
        collector,
    )

    log_result = tools[0].invoke({"service_name": "order-service", "lookback_minutes": 15})
    metric_result = tools[1].invoke({"service_name": "order-service", "lookback_minutes": 15})
    change_result = tools[2].invoke({"service_name": "order-service", "lookback_hours": 6})

    assert log_result[0]["evidence_id"] == "log-order-runtime-error"
    assert metric_result[0]["evidence_id"] == "metric-order-error-spike"
    assert change_result[0]["evidence_id"] == "change-order-release-rc1"
    assert collector.returned_ids == RELEASE_500_SCENARIO.required_evidence_ids
    assert collector.tool_call_count == 3
    assert collector.called_tool_names == INVESTIGATION_TOOL_NAMES


def test_read_only_tools_emit_bounded_spans_without_evidence_content() -> None:
    exporter = InMemorySpanExporter()
    runtime = create_tracing_runtime(
        "tool-test",
        settings=TracingSettings(enabled=True),
        span_exporter=exporter,
        instrument_httpx=False,
    )
    tools = create_investigation_tools(
        FrozenScenarioDataSource(RELEASE_500_SCENARIO),
        EvidenceCollector(),
        tracer=runtime.get_tracer(__name__),
    )

    tools[0].invoke({"service_name": "order-service", "lookback_minutes": 15})

    spans = exporter.get_finished_spans()
    assert [span.name for span in spans] == ["opsswarm.tool.query_service_logs"]
    assert spans[0].attributes["gen_ai.tool.name"] == "query_service_logs"
    assert spans[0].attributes["opsswarm.tool.evidence_count"] == 1
    assert all("summary" not in key for key in spans[0].attributes)
    runtime.shutdown()


def test_tool_rejects_unbounded_lookback() -> None:
    tools = create_investigation_tools(
        FrozenScenarioDataSource(RELEASE_500_SCENARIO),
        EvidenceCollector(),
    )

    with pytest.raises(ValidationError):
        tools[0].invoke({"service_name": "order-service", "lookback_minutes": 61})


def test_frozen_source_rejects_unknown_service() -> None:
    tools = create_investigation_tools(
        FrozenScenarioDataSource(RELEASE_500_SCENARIO),
        EvidenceCollector(),
    )

    with pytest.raises(UnsupportedInvestigationTarget):
        tools[0].invoke({"service_name": "unknown-service", "lookback_minutes": 15})
