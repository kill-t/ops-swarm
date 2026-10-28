"""离线验证回流内收范围、完整材料、专业调用及取证失败关闭。"""

import asyncio
import json
from datetime import timedelta
from threading import Event, get_ident
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx2
import pytest
from pydantic import ValidationError

from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.errors import InvestigationDataSourceFailure
from opsswarm.investigation.models import EvidenceObservation, SpecialistReport
from opsswarm.investigation.query_execution import (
    EvidenceQueryCapacityExceeded,
    EvidenceQueryTimeout,
    ReadOnlyQueryExecutor,
)
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_dispatch.policy import task_id
from opsswarm.investigation.reinvestigation_execution.context import (
    PROMPT,
    build_packet,
    validate_report,
)
from opsswarm.investigation.reinvestigation_execution.evidence import ExecutionEvidenceSource
from opsswarm.investigation.reinvestigation_execution.model import ExecutionModel
from opsswarm.investigation.reinvestigation_execution.window import effective_window
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot, evidence_hash
from opsswarm.telemetry_mcp.client import TelemetryMcpLogDataSource
from opsswarm.telemetry_mcp.server import TelemetryMcpServerSettings, create_app
from opsswarm.telemetry_metrics_mcp.client import TelemetryMcpMetricDataSource
from opsswarm.telemetry_metrics_mcp.server import (
    TelemetryMetricsMcpServerSettings,
)
from opsswarm.telemetry_metrics_mcp.server import (
    create_app as create_metrics_app,
)
from tests.model_budget_helpers import sdk_model, sdk_response
from tests.unit.investigation.test_reflow_context_policy import NOW
from tests.unit.investigation.test_reinvestigation_dispatch_policy import (
    compile_result,
    dispatch_source,
)
from tests.unit.telemetry_mcp.test_client import END, START, FixedSource
from tests.unit.telemetry_metrics_mcp.test_contracts import END as METRIC_END
from tests.unit.telemetry_metrics_mcp.test_contracts import START as METRIC_START
from tests.unit.telemetry_metrics_mcp.test_contracts import metric_evidence
from tests.unit.telemetry_metrics_mcp.test_server import RecordingSource


def assignment_for(role="log", *, start=NOW, end=None):
    return TaskAssignment(
        task_key=f"followup_{role}",
        role=role,
        goal="调查恢复失败并保留未解问题",
        service_name="order-service",
        window_start=start,
        window_end=end if end is not None else start + timedelta(minutes=2),
    )


def observation(assignment, window, **changes):
    return EvidenceObservation(
        **(
            {
                "evidence_id": f"new-{assignment.role.value}",
                "kind": assignment.role,
                "source": {
                    "log": "loki-fault-lab",
                    "metric": "prometheus-fault-lab",
                    "change": "change-simulator",
                }[assignment.role.value],
                "summary": "新窗口仍有异常，需要继续核查",
                "observed_at": window.window.window_end,
                "attributes": {
                    "service_name": assignment.service_name,
                    "window_start": window.window.window_start.isoformat(),
                    "window_end": window.window.window_end.isoformat(),
                },
            }
            | changes
        )
    )


def material_fixture():
    source = dispatch_source(floor=NOW - timedelta(seconds=80, microseconds=100000))
    dispatch = compile_result(source, now=NOW + timedelta(seconds=100, microseconds=700000))
    assignment = dispatch.assignment.tasks[0]
    identity = task_id(source.investigation_run_id, assignment.task_key)
    window = effective_window(assignment)
    snapshot = TaskEvidenceSnapshot(
        version="task-evidence-live-mcp-v1",
        data_source_mode="live",
        evidence_snapshot_id=f"reflow-v1:{identity}",
        role=assignment.role,
        service_name=assignment.service_name,
        window_start=window.window.window_start,
        window_end=window.window.window_end,
        evidence=(observation(assignment, window),),
    )
    return source, dispatch, assignment, identity, window, snapshot


def report_for(snapshot, **changes):
    return SpecialistReport(
        **(
            {
                "specialist": snapshot.role,
                "summary": "目前证据不足以证明根因",
                "findings": ("需要保留未解问题，不能沿用旧批准",),
                "evidence_ids": tuple(e.evidence_id for e in snapshot.evidence),
                "confidence": 0.2,
            }
            | changes
        )
    )


@pytest.mark.parametrize("role", ["log", "metric", "change"])
def test_microseconds_and_remainders_are_only_removed_inward(role):
    assignment = assignment_for(
        role,
        start=NOW + timedelta(microseconds=1),
        end=NOW + timedelta(seconds=181, microseconds=9),
    )
    window = effective_window(assignment)
    assert window.requested_start == assignment.window_start
    assert window.requested_end == assignment.window_end
    assert window.adjustments == ("round_seconds",)
    assert window.window.window_start == NOW + timedelta(seconds=1)
    assert window.window.window_end == NOW + timedelta(seconds=181)
    assert assignment.window_start <= window.window.window_start < window.window.window_end
    assert window.window.window_end <= assignment.window_end
    if role != "change":
        assignment = assignment.model_copy(update={"window_end": NOW + timedelta(seconds=180)})
        adjusted = effective_window(assignment)
        assert adjusted.adjustments == ("round_seconds", "inward_minutes")
        assert adjusted.window.window_start == NOW + timedelta(seconds=60)


@pytest.mark.parametrize("role,seconds", [("log", 59), ("metric", 0.999), ("change", 0.999)])
def test_too_short_window_blocks_without_expanding_or_requesting(role, seconds):
    assignment = assignment_for(role, start=NOW, end=NOW + timedelta(seconds=seconds))
    with pytest.raises(ReflowError) as caught:
        effective_window(assignment)
    assert caught.value.code == "execution_window_too_short"


def test_exact_whole_window_has_no_adjustments():
    window = effective_window(assignment_for())
    assert window.adjustments == ()


def test_complete_input_preserves_historical_constraints_and_unobserved_scope():
    values = material_fixture()
    source, dispatch, assignment, identity, window, snapshot = values
    packet = build_packet(*values)
    payload = json.loads(packet.model_input.messages[1].content)
    assert packet.model_input.messages[0].content == PROMPT
    assert payload == {
        "source": source.model_dump(mode="json"),
        "dispatch_sha256": content_hash(dispatch),
        "task_id": str(identity),
        "assignment": assignment.model_dump(mode="json"),
        "observation_window": window.model_dump(mode="json"),
        "new_evidence": snapshot.model_dump(mode="json"),
    }
    assert packet.manifest.source_sha256 == content_hash(source)
    assert packet.manifest.assignment_sha256 == content_hash(assignment)
    assert packet.manifest.evidence_sha256 == evidence_hash(snapshot)
    assert packet.manifest.input_sha256 == packet.allocation.input_sha256
    assert packet.manifest.tool_names == packet.manifest.trimming == ()
    assert packet.model_input.output_schema["properties"].keys() == {
        "specialist",
        "summary",
        "findings",
        "evidence_ids",
        "confidence",
    }


@pytest.mark.parametrize("tamper", ["source", "task", "window", "snapshot", "material"])
def test_context_rejects_cross_scope_and_corrupted_historical_material(tamper):
    source, dispatch, assignment, identity, window, snapshot = material_fixture()
    if tamper == "source":
        dispatch = dispatch.model_copy(update={"source_sha256": "a" * 64})
    elif tamper == "task":
        identity = uuid4()
    elif tamper == "window":
        window = window.model_copy(update={"adjustments": ()})
    elif tamper == "snapshot":
        snapshot = snapshot.model_copy(update={"evidence_snapshot_id": "old-frozen-evidence"})
    else:
        first, *rest = source.materials
        source = source.model_copy(
            update={"materials": (first.model_copy(update={"content": {}}), *rest)}
        )
        dispatch = dispatch.model_copy(update={"source_sha256": content_hash(source)})
    with pytest.raises(ReflowError):
        build_packet(source, dispatch, assignment, identity, window, snapshot)


def test_necessary_context_over_budget_stops_instead_of_trimming():
    source, dispatch, *rest = material_fixture()
    policy = source.model_budget.model_copy(update={"max_input_tokens_per_call": 1})
    source = source.model_copy(update={"model_budget": policy})
    dispatch = dispatch.model_copy(update={"source_sha256": content_hash(source)})
    with pytest.raises(CallBudgetExceeded) as caught:
        build_packet(source, dispatch, *rest)
    assert caught.value.code == "model_input_budget_exceeded"


@pytest.mark.parametrize("tamper", ["role", "reference"])
def test_professional_report_may_only_reference_its_current_snapshot(tamper):
    _, _, assignment, _, _, snapshot = material_fixture()
    changes = {"specialist": "metric"} if tamper == "role" else {"evidence_ids": ("old-log",)}
    with pytest.raises(ReflowError) as caught:
        validate_report(assignment, snapshot, report_for(snapshot, **changes))
    assert caught.value.code == "execution_report_invalid"


def test_empty_new_evidence_can_have_honest_report_without_root_cause_mapping():
    source, dispatch, assignment, identity, window, snapshot = material_fixture()
    snapshot = snapshot.model_copy(update={"evidence": ()})
    packet = build_packet(source, dispatch, assignment, identity, window, snapshot)
    validate_report(assignment, snapshot, report_for(snapshot))
    assert json.loads(packet.model_input.messages[1].content)["new_evidence"]["evidence"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["log", "metric", "change"])
async def test_source_routes_only_assigned_port_and_validates_exact_live_scope(role):
    assignment = assignment_for(role)
    window, identity = effective_window(assignment), uuid4()
    item = observation(assignment, window)
    logs = SimpleNamespace(query_logs_in_window_async=AsyncMock(return_value=(item,)))
    metrics = SimpleNamespace(query_metrics_in_window_async=AsyncMock(return_value=(item,)))
    calls = []
    main_thread = get_ident()

    def change_query(**kwargs):
        calls.append((kwargs, get_ident()))
        return (item,)

    source = ExecutionEvidenceSource(
        logs=logs, metrics=metrics, changes=SimpleNamespace(list_changes_in_window=change_query)
    )
    try:
        snapshot = await source.collect(assignment, identity, window)
        assert snapshot.evidence == (item,)
        assert snapshot.evidence_snapshot_id == f"reflow-v1:{identity}"
        assert logs.query_logs_in_window_async.await_count == (role == "log")
        assert metrics.query_metrics_in_window_async.await_count == (role == "metric")
        assert len(calls) == (role == "change")
        if calls:
            assert calls[0] == (
                {"service_name": assignment.service_name, "window": window.window},
                calls[0][1],
            )
            assert calls[0][1] != main_thread
    finally:
        source.close()


@pytest.mark.asyncio
async def test_source_failure_never_falls_back_or_creates_old_evidence():
    assignment = assignment_for()
    failure = InvestigationDataSourceFailure(
        error_code="telemetry_mcp_unavailable", message="来源失败"
    )
    logs = SimpleNamespace(query_logs_in_window_async=AsyncMock(side_effect=failure))
    metrics = SimpleNamespace(query_metrics_in_window_async=AsyncMock())
    source = ExecutionEvidenceSource(logs=logs, metrics=metrics)
    try:
        with pytest.raises(InvestigationDataSourceFailure) as caught:
            await source.collect(assignment, uuid4(), effective_window(assignment))
        assert caught.value is failure
        metrics.query_metrics_in_window_async.assert_not_awaited()
    finally:
        source.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [{"source": "frozen"}, {"kind": "metric"}, {"attributes": {}}])
async def test_source_rejects_wrong_origin_role_or_window_attributes(changes):
    assignment = assignment_for()
    window = effective_window(assignment)
    logs = SimpleNamespace(
        query_logs_in_window_async=AsyncMock(
            return_value=(observation(assignment, window, **changes),)
        )
    )
    source = ExecutionEvidenceSource(logs=logs)
    try:
        with pytest.raises(ValidationError):
            await source.collect(assignment, uuid4(), window)
    finally:
        source.close()


@pytest.mark.asyncio
async def test_timed_out_change_query_keeps_occupied_slot_and_cannot_publish_later():
    assignment = assignment_for("change")
    started, release = Event(), Event()
    executor = ReadOnlyQueryExecutor(max_queries=1, timeout_seconds=0.02)

    def blocked_query(**kwargs):
        started.set()
        release.wait(timeout=2)
        return ()

    source = ExecutionEvidenceSource(
        changes=SimpleNamespace(list_changes_in_window=blocked_query), executor=executor
    )
    try:
        with pytest.raises(EvidenceQueryTimeout):
            await source.collect(assignment, uuid4(), effective_window(assignment))
        assert started.is_set()
        with pytest.raises(EvidenceQueryCapacityExceeded):
            await source.collect(assignment, uuid4(), effective_window(assignment))
    finally:
        release.set()
        source.close()


@pytest.mark.asyncio
async def test_existing_log_mcp_protocol_accepts_inward_window_over_asgi():
    assignment = assignment_for(start=START, end=END)
    settings = TelemetryMcpServerSettings(
        host="127.0.0.1", port=18004, allowed_hosts=("127.0.0.1:18004",)
    )
    app = create_app(settings=settings, source=FixedSource())
    source = ExecutionEvidenceSource(
        logs=TelemetryMcpLogDataSource(transport=httpx2.ASGITransport(app=app))
    )
    try:
        async with app.router.lifespan_context(app):
            snapshot = await source.collect(assignment, uuid4(), effective_window(assignment))
        assert len(snapshot.evidence) == 1
        assert snapshot.evidence[0].source == "loki-fault-lab"
    finally:
        source.close()


@pytest.mark.asyncio
async def test_existing_metric_mcp_protocol_validates_aggregation_over_asgi():
    assignment = assignment_for("metric", start=METRIC_START, end=METRIC_END)
    settings = TelemetryMetricsMcpServerSettings(
        host="127.0.0.1", port=18005, allowed_hosts=("127.0.0.1:18005",)
    )
    app = create_metrics_app(settings=settings, source=RecordingSource((metric_evidence(),)))
    source = ExecutionEvidenceSource(
        metrics=TelemetryMcpMetricDataSource(transport=httpx2.ASGITransport(app=app))
    )
    try:
        async with app.router.lifespan_context(app):
            snapshot = await source.collect(assignment, uuid4(), effective_window(assignment))
        assert snapshot.evidence == (metric_evidence(),)
    finally:
        source.close()


@pytest.mark.asyncio
async def test_async_source_limits_three_concurrent_queries():
    assignment = assignment_for()
    window = effective_window(assignment)
    active = 0
    maximum = 0
    three_started = asyncio.Event()
    release = asyncio.Event()

    async def query(**kwargs):
        nonlocal active, maximum
        active += 1
        maximum = max(maximum, active)
        if active == 3:
            three_started.set()
        try:
            await release.wait()
            return ()
        finally:
            active -= 1

    source = ExecutionEvidenceSource(logs=SimpleNamespace(query_logs_in_window_async=query))
    tasks = [asyncio.create_task(source.collect(assignment, uuid4(), window)) for _ in range(4)]
    try:
        await asyncio.wait_for(three_started.wait(), timeout=1)
        assert maximum == active == 3
        release.set()
        snapshots = await asyncio.gather(*tasks)
        assert all(snapshot.evidence == () for snapshot in snapshots)
        assert maximum == 3
    finally:
        release.set()
        await asyncio.gather(*tasks)
        source.close()


@pytest.mark.asyncio
async def test_excessive_evidence_never_becomes_snapshot():
    assignment = assignment_for()
    window = effective_window(assignment)
    item = observation(assignment, window)
    source = ExecutionEvidenceSource(
        logs=SimpleNamespace(query_logs_in_window_async=AsyncMock(return_value=(item,) * 21))
    )
    try:
        with pytest.raises(ReflowError) as caught:
            await source.collect(assignment, uuid4(), window)
        assert caught.value.code == "execution_evidence_invalid"
    finally:
        source.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("usage_mode", ["reported", "missing", "partial", "invalid", "parse_error"])
async def test_real_sdk_freezes_professional_schema_and_preserves_reported_or_unknown_usage(
    usage_mode,
):
    values = material_fixture()
    packet = build_packet(*values)
    report = report_for(values[-1])
    requests = []

    def reply(request):
        payload = json.loads(request.content)
        requests.append(payload)
        response = sdk_response(
            payload,
            report.model_dump(mode="json"),
            usage=usage_mode != "missing",
            invalid_json=usage_mode == "parse_error",
        )
        body = response.json()
        if usage_mode == "partial":
            body["usage"] = {"prompt_tokens": 80}
        elif usage_mode == "invalid":
            body["usage"]["total_tokens"] = 999
        return httpx2.Response(200, json=body)

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client:
        caller = ExecutionModel(sdk_model(client), values[0].model_budget)
        if usage_mode == "invalid":
            with pytest.raises(CallBudgetExceeded) as caught:
                await caller.invoke(packet.model_input)
            assert caught.value.code == "model_usage_invalid"
        elif usage_mode == "parse_error":
            with pytest.raises(ValueError):
                await caller.invoke(packet.model_input)
            assert caller.usage.total_tokens == 100
        else:
            result = await caller.invoke(packet.model_input)
            assert result.report == report
            if usage_mode == "reported":
                assert caller.usage.total_tokens == 100
            else:
                assert caller.usage is None
        assert len(requests) == 1
        assert requests[0]["tools"] == [packet.model_input.tool_schema]
        assert requests[0]["messages"] == [
            message.model_dump() for message in packet.model_input.messages
        ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [{"output_schema": {}}, {"tool_schema": {}}, {"structured_output_method": "json_schema"}],
)
async def test_wrong_schema_or_method_never_reaches_supplier(changes):
    values = material_fixture()
    packet = build_packet(*values)
    async with httpx2.AsyncClient(
        transport=httpx2.MockTransport(lambda _: pytest.fail("错误契约不能调用"))
    ) as client:
        caller = ExecutionModel(sdk_model(client), values[0].model_budget)
        with pytest.raises(CallBudgetExceeded):
            await caller.invoke(packet.model_input.model_copy(update=changes))
        assert caller.usage is None


@pytest.mark.asyncio
async def test_usage_is_reset_before_each_attempt_including_invalid_input():
    values = material_fixture()
    packet = build_packet(*values)
    report = report_for(values[-1])

    def reply(request):
        return sdk_response(json.loads(request.content), report.model_dump(mode="json"))

    async with httpx2.AsyncClient(transport=httpx2.MockTransport(reply)) as client:
        caller = ExecutionModel(sdk_model(client), values[0].model_budget)
        await caller.invoke(packet.model_input)
        assert caller.usage.total_tokens == 100
        with pytest.raises(CallBudgetExceeded):
            await caller.invoke(packet.model_input.model_copy(update={"output_schema": {}}))
        assert caller.usage is None


@pytest.mark.asyncio
async def test_closed_or_altered_source_does_not_issue_requests():
    assignment = assignment_for()
    window = effective_window(assignment)
    logs = SimpleNamespace(query_logs_in_window_async=AsyncMock())
    source = ExecutionEvidenceSource(logs=logs)
    altered = window.model_copy(update={"adjustments": ("forged",)})
    with pytest.raises(ReflowError):
        await source.collect(assignment, uuid4(), altered)
    source.close()
    with pytest.raises(ReflowError):
        await source.collect(assignment, uuid4(), window)
    logs.query_logs_in_window_async.assert_not_awaited()
