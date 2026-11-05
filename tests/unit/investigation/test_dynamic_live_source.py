"""现场动态查询使用任务精确窗口，且不能替换来源或扩大范围。"""

from datetime import UTC, datetime, timedelta
from threading import get_ident
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest

from opsswarm.investigation.changes import ChangeApiOnlyInvestigationDataSource
from opsswarm.investigation.dynamic_live_source import DynamicLiveDataSource
from opsswarm.investigation.dynamic_third_round_routing_graph import DynamicThirdRoundRoutingGraph
from opsswarm.investigation.live import LiveInvestigationDataSource
from opsswarm.investigation.live_window import UnsupportedLiveTaskSource
from opsswarm.investigation.models import EvidenceKind, EvidenceObservation
from opsswarm.investigation.query_execution import ReadOnlyQueryExecutor
from opsswarm.investigation.tasks.contracts import TaskAssignment
from opsswarm.investigation.tasks.errors import TaskFactConflict

CUTOFF = datetime(2025, 1, 1, 12, tzinfo=UTC)


class Ports:
    def __init__(self):
        self.calls = []
        self.error = None
        self.forged_source = False

    def read(self, role, service_name, window):
        self.calls.append((role, service_name, window, get_ident()))
        if self.error:
            raise self.error
        return (
            EvidenceObservation(
                evidence_id="precise-live-" + role.value,
                kind=role,
                source="frozen-log-store"
                if self.forged_source
                else {
                    EvidenceKind.LOG: "loki-fault-lab",
                    EvidenceKind.METRIC: "prometheus-fault-lab",
                    EvidenceKind.CHANGE: "change-simulator",
                }[role],
                summary="固定端口响应，不是现场演示证据",
                observed_at=window.window_end,
                attributes={
                    "service_name": service_name,
                    "window_start": window.window_start.isoformat(),
                    "window_end": window.window_end.isoformat(),
                },
            ),
        )

    def query_logs_in_window(self, *, service_name, window):
        return self.read(EvidenceKind.LOG, service_name, window)

    def query_metrics_in_window(self, *, service_name, window):
        return self.read(EvidenceKind.METRIC, service_name, window)

    def list_changes_in_window(self, *, service_name, window):
        return self.read(EvidenceKind.CHANGE, service_name, window)


@pytest.fixture
def source():
    ports = Ports()
    executor = ReadOnlyQueryExecutor()
    try:
        yield (
            DynamicLiveDataSource(
                logs=ports,
                metrics=ports,
                changes=cast(ChangeApiOnlyInvestigationDataSource, ports),
                service_name="order-service",
                cutoff=CUTOFF,
                snapshot_id="test-live-snapshot",
                executor=executor,
            ),
            ports,
        )
    finally:
        executor.close()


def assignment(role=EvidenceKind.LOG, seconds=120, **changes):
    data = dict(
        task_key=role.value,
        role=role,
        goal="核对当前专业证据",
        service_name="order-service",
        window_start=CUTOFF - timedelta(seconds=seconds),
        window_end=CUTOFF,
        evidence_snapshot_id="test-live-snapshot",
    )
    return TaskAssignment.model_validate(data | changes)


@pytest.mark.asyncio
@pytest.mark.parametrize("role", list(EvidenceKind))
async def test_exact_task_window_is_used_in_bounded_thread(source, role):
    reader, ports = source
    task = assignment(role)
    evidence = await reader.collect(task)
    assert len(ports.calls) == 1
    called_role, service, window, thread = ports.calls[0]
    assert (called_role, service) == (role, task.service_name)
    assert (window.window_start, window.window_end) == (task.window_start, task.window_end)
    assert thread != get_ident()
    assert evidence[0].attributes["window_start"] == task.window_start.isoformat()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"seconds": 61},
        {"seconds": 960},
        {"service_name": "other-service"},
        {"window_end": CUTOFF - timedelta(seconds=1)},
        {"evidence_snapshot_id": "other-run"},
        {"role": EvidenceKind.CHANGE, "seconds": 21660},
    ],
)
async def test_invalid_scope_is_rejected_before_external_request(source, changes):
    reader, ports = source
    with pytest.raises(TaskFactConflict, match="完整分钟窗口"):
        await reader.collect(assignment(**changes))
    assert ports.calls == []


@pytest.mark.asyncio
async def test_source_failure_and_frozen_material_are_not_silently_substituted(source):
    reader, ports = source
    ports.error = RuntimeError("telemetry-unavailable")
    with pytest.raises(RuntimeError, match="telemetry-unavailable"):
        await reader.collect(assignment())
    assert len(ports.calls) == 1
    ports.error, ports.forged_source = None, True
    with pytest.raises(ValueError, match="来源"):
        await reader.collect(assignment())


def test_dynamic_binding_requires_both_mcp_ports(source):
    reader, ports = source
    with pytest.raises(UnsupportedLiveTaskSource, match="完整MCP"):
        LiveInvestigationDataSource(mcp_logs=ports).bind_dynamic_window(
            run_id=uuid4(),
            service_name="order-service",
            cutoff=CUTOFF,
            executor=reader._executor,
        )


@pytest.mark.parametrize("bound,other", [("v15", "v16"), ("v16", "v15")])
def test_checkpoint_versions_cannot_be_cross_restored(bound, other):
    graph = object.__new__(DynamicThirdRoundRoutingGraph)
    graph._graph_version = "dynamic-investigation-graph-" + bound
    checkpoint = SimpleNamespace(
        metadata={
            "workflow_version": "v3",
            "graph_version": "dynamic-investigation-graph-" + other,
        },
        checkpoint={
            "channel_values": {
                "workflow_version": "v3",
                "graph_version": "dynamic-investigation-graph-" + other,
            }
        },
    )
    with pytest.raises(RuntimeError, match="图版本不兼容"):
        graph._validate_checkpoint_contract(checkpoint)
