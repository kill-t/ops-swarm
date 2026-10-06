"""真实 Streamable HTTP MCP 进程接入现场 v2 的日志专业任务。"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from contextlib import contextmanager

import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.models import EvidenceKind, InvestigationDataSourceMode
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.workflow_versions import WorkflowVersion
from opsswarm.telemetry_mcp.client import (
    TelemetryMcpClientSettings,
    TelemetryMcpLogDataSource,
)
from opsswarm.telemetry_metrics_mcp.client import (
    TelemetryMcpMetricDataSource,
    TelemetryMetricsMcpClientSettings,
)
from tests.integration.test_live_task_graph import run_live_worker
from tests.live_task_helpers import LiveHttpFixture
from tests.task_facts_helpers import (
    TaskClock,
    mark_run_as_legacy_live,
    mark_run_as_mcp_log_live,
    started_run,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
LIVE = InvestigationDataSourceMode.LIVE


class ForbiddenMcpLogs:
    def query_logs_in_window(self, **kwargs):
        del kwargs
        raise AssertionError("历史现场策略不能调用 MCP")


@contextmanager
def telemetry_mcp_process():
    """只向测试 MCP 子进程传递 Python 运行所需环境，不传业务凭据。"""

    allowed = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "PATH",
            "PATHEXT",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "WINDIR",
            "PYTHONPATH",
        }
    }
    process = subprocess.Popen(
        [sys.executable, "tests/telemetry_mcp_process.py"],
        cwd=os.getcwd(),
        env=allowed,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("测试 Telemetry MCP 进程提前退出")
            try:
                with socket.create_connection(("127.0.0.1", 18004), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("测试 Telemetry MCP 进程未按时启动")
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def mcp_source() -> TelemetryMcpLogDataSource:
    return TelemetryMcpLogDataSource(
        TelemetryMcpClientSettings(
            endpoint_url="http://127.0.0.1:18004/mcp",
            request_timeout_seconds=1,
        )
    )


@contextmanager
def telemetry_metrics_mcp_process():
    """启动不含业务凭据的真实指标 MCP 测试进程。"""

    allowed = {
        key: value
        for key, value in os.environ.items()
        if key.upper()
        in {
            "PATH",
            "PATHEXT",
            "SYSTEMDRIVE",
            "SYSTEMROOT",
            "TEMP",
            "TMP",
            "WINDIR",
            "PYTHONPATH",
        }
    }
    process = subprocess.Popen(
        [sys.executable, "tests/telemetry_metrics_mcp_process.py"],
        cwd=os.getcwd(),
        env=allowed,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("测试指标 MCP 进程提前退出")
            try:
                with socket.create_connection(("127.0.0.1", 18005), timeout=0.1):
                    break
            except OSError:
                time.sleep(0.05)
        else:
            raise RuntimeError("测试指标 MCP 进程未按时启动")
        yield
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def metric_mcp_source() -> TelemetryMcpMetricDataSource:
    return TelemetryMcpMetricDataSource(
        TelemetryMetricsMcpClientSettings(
            endpoint_url="http://127.0.0.1:18005/mcp",
            request_timeout_seconds=1,
        )
    )


async def test_new_live_worker_reads_logs_and_metrics_from_real_mcp_processes(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        data_source_mode=LIVE,
    )
    clock = TaskClock(started.run.observation_cutoff_at)
    fixture = LiveHttpFixture(
        started.run.observation_cutoff_at,
        clock,
        mcp_logs=mcp_source(),
        mcp_metrics=metric_mcp_source(),
    )

    with telemetry_mcp_process(), telemetry_metrics_mcp_process():
        assert await run_live_worker(
            database_session_factory,
            checkpoint_database_url,
            store,
            clock,
            fixture.source,
        )

    run = await store.get_run(started.run.id)
    assert run.status == "succeeded"
    assert run.evidence_policy_version == "task-evidence-live-mcp-v1"
    assert {kind for kind, _ in fixture.requests} == {"change"}
    tasks = (
        (await CollaborationQueries(database_session_factory).get_snapshot(run.id)).rounds[0].tasks
    )
    telemetry_tasks = [
        task for task in tasks if task.assignment.role in {EvidenceKind.LOG, EvidenceKind.METRIC}
    ]
    saved = [
        await CollaborationQueries(database_session_factory).get_evidence(run.id, task.id)
        for task in telemetry_tasks
    ]
    assert all(record is not None for record in saved)
    assert {record.snapshot.version for record in saved if record is not None} == {
        "task-evidence-live-mcp-v1"
    }
    assert {record.snapshot.evidence[0].source for record in saved if record is not None} == {
        "loki-fault-lab",
        "prometheus-fault-lab",
    }
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(run.id)
    telemetry_calls = [
        call for call in budget.calls if call.operation in {"query_logs", "query_metrics"}
    ]
    assert {call.operation for call in telemetry_calls if call.status == "succeeded"} == {
        "query_logs",
        "query_metrics",
    }


async def test_metric_mcp_unavailable_fails_without_direct_fallback(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        data_source_mode=LIVE,
        max_attempts=1,
    )
    clock = TaskClock(started.run.observation_cutoff_at)
    fixture = LiveHttpFixture(
        started.run.observation_cutoff_at,
        clock,
        mcp_logs=mcp_source(),
        mcp_metrics=metric_mcp_source(),
    )

    with telemetry_mcp_process():
        assert await run_live_worker(
            database_session_factory,
            checkpoint_database_url,
            store,
            clock,
            fixture.source,
        )

    run = await store.get_run(started.run.id)
    assert run.status == "failed" and run.result is None
    assert "metric" not in {kind for kind, _ in fixture.requests}
    tasks = (
        (await CollaborationQueries(database_session_factory).get_snapshot(run.id)).rounds[0].tasks
    )
    metric = next(task for task in tasks if task.assignment.role is EvidenceKind.METRIC)
    assert (
        await CollaborationQueries(database_session_factory).get_evidence(run.id, metric.id) is None
    )
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(run.id)
    metric_call = next(call for call in budget.calls if call.operation == "query_metrics")
    assert metric_call.status == "failed"
    assert metric_call.error_code == "telemetry_mcp_timeout"


async def test_live_worker_persists_log_evidence_from_real_mcp_process(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        data_source_mode=LIVE,
    )
    await mark_run_as_mcp_log_live(database_session_factory, started.run.id)
    clock = TaskClock(started.run.observation_cutoff_at)
    fixture = LiveHttpFixture(
        started.run.observation_cutoff_at,
        clock,
        mcp_logs=mcp_source(),
    )

    with telemetry_mcp_process():
        assert await run_live_worker(
            database_session_factory,
            checkpoint_database_url,
            store,
            clock,
            fixture.source,
        )

    run = await store.get_run(started.run.id)
    assert run.status == "succeeded"
    assert run.evidence_policy_version == "task-evidence-live-mcp-log-v1"
    assert {kind for kind, _ in fixture.requests} == {"metric", "change"}
    tasks = (
        (await CollaborationQueries(database_session_factory).get_snapshot(run.id)).rounds[0].tasks
    )
    log = next(task for task in tasks if task.assignment.role is EvidenceKind.LOG)
    saved = await CollaborationQueries(database_session_factory).get_evidence(run.id, log.id)
    assert saved is not None
    assert saved.snapshot.version == "task-evidence-live-mcp-log-v1"
    assert saved.snapshot.evidence[0].evidence_id.startswith("mcp-log-")
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(run.id)
    log_call = next(call for call in budget.calls if call.operation == "query_logs")
    assert log_call.status == "succeeded" and saved.call_id == log_call.id


async def test_mcp_handshake_timeout_fails_without_direct_or_frozen_fallback(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        data_source_mode=LIVE,
        max_attempts=1,
    )
    await mark_run_as_mcp_log_live(database_session_factory, started.run.id)
    clock = TaskClock(started.run.observation_cutoff_at)
    fixture = LiveHttpFixture(
        started.run.observation_cutoff_at,
        clock,
        mcp_logs=mcp_source(),
    )

    assert await run_live_worker(
        database_session_factory,
        checkpoint_database_url,
        store,
        clock,
        fixture.source,
    )

    run = await store.get_run(started.run.id)
    assert run.status == "failed" and run.result is None
    assert {kind for kind, _ in fixture.requests} <= {"metric", "change"}
    assert (await store.get_evidence_board(run.id)).evidence_items == ()
    tasks = (
        (await CollaborationQueries(database_session_factory).get_snapshot(run.id)).rounds[0].tasks
    )
    log = next(task for task in tasks if task.assignment.role is EvidenceKind.LOG)
    assert await CollaborationQueries(database_session_factory).get_evidence(run.id, log.id) is None
    budget = await CallBudgetQueries(database_session_factory).get_snapshot(run.id)
    log_call = next(call for call in budget.calls if call.operation == "query_logs")
    assert log_call.status == "failed"
    assert log_call.error_code == "telemetry_mcp_timeout"


async def test_legacy_live_policy_keeps_direct_loki_path(
    database_session_factory,
    checkpoint_database_url,
) -> None:
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await started_run(
        database_session_factory,
        workflow_version=WorkflowVersion.V2,
        data_source_mode=LIVE,
    )
    await mark_run_as_legacy_live(database_session_factory, started.run.id)
    clock = TaskClock(started.run.observation_cutoff_at)
    fixture = LiveHttpFixture(
        started.run.observation_cutoff_at,
        clock,
        mcp_logs=ForbiddenMcpLogs(),
    )

    assert await run_live_worker(
        database_session_factory,
        checkpoint_database_url,
        store,
        clock,
        fixture.source,
    )

    run = await store.get_run(started.run.id)
    assert run.status == "succeeded"
    assert run.evidence_policy_version == "task-evidence-live-v1"
    assert {kind for kind, _ in fixture.requests} == {"log", "metric", "change"}
