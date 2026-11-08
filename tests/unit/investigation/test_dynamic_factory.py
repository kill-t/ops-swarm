"""当前图装配的依赖边界、旧导入兼容与持久身份绑定。"""

import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from opsswarm.investigation.budget.contracts import CallBudgetPolicy
from opsswarm.investigation.coordination.contracts import CoordinationPolicy
from opsswarm.investigation.dynamic_live_source import DynamicLiveDataSource
from opsswarm.investigation.graph_thread import build_graph_thread_id
from opsswarm.investigation.live import LiveInvestigationDataSource
from opsswarm.investigation.models import (
    InvestigationDataSourceMode,
    InvestigationMode,
    InvestigationRequest,
    WorkItemKind,
)
from opsswarm.investigation.tasks.evidence import live_snapshot_id
from opsswarm.investigation.workflow_versions import (
    DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    LIVE_DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    WorkflowVersion,
)
from opsswarm.worker.dynamic_factory import CurrentDynamicGraphFactory
from opsswarm.worker.runner import ProcessorContext
from tests.model_budget_helpers import model_policy


@pytest.mark.parametrize("entry", ["port", "factory"])
def test_current_entry_does_not_load_legacy_workflow_implementations(entry):
    source = Path(__file__).resolve().parents[3] / "src"
    code = f"""
import sys
sys.path.insert(0, {str(source)!r})
if {entry!r} == 'port':
    from opsswarm.investigation.coordination.ports import FirstRoundPlanner
    for prefix in ('langchain', 'langchain_core', 'langchain_openai', 'langgraph', 'fastapi'):
        assert not any(n == prefix or n.startswith(prefix + '.') for n in sys.modules), prefix
else:
    from opsswarm.worker.dynamic_factory import CurrentDynamicGraphFactory
    forbidden = (
        'opsswarm.worker.dynamic_graph',
        'opsswarm.investigation.dynamic_graph',
        'opsswarm.investigation.dynamic_first_round_graph',
        'opsswarm.investigation.dynamic_third_round_review_graph',
    )
    assert not set(forbidden).intersection(sys.modules), set(forbidden).intersection(sys.modules)
"""
    result = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_legacy_import_paths_keep_the_same_factory_and_planning_port():
    from opsswarm.investigation.coordination.ports import FirstRoundPlanner
    from opsswarm.investigation.dynamic_graph import FirstRoundPlanner as old_port
    from opsswarm.worker import dynamic_graph as old
    from opsswarm.worker.dynamic_factory import DynamicThirdRoundRoutingGraphFactory

    assert old.DynamicThirdRoundRoutingGraphFactory is DynamicThirdRoundRoutingGraphFactory
    assert old_port is FirstRoundPlanner
    assert old.DynamicCoordinationGraphFactory.__module__ == old.__name__
    assert old.DynamicThirdRoundReviewGraphFactory.__module__ == old.__name__


def context(*, live):
    run_id = uuid4()
    policy = model_policy()
    policy = policy.model_copy(
        update={
            "profile": policy.profile.model_copy(
                update={"structured_output_method": "function_calling"}
            )
        }
    )
    started_at = datetime(2026, 1, 1, tzinfo=UTC)
    return ProcessorContext(
        request=InvestigationRequest(
            incident_id=uuid4(),
            service_name="order-service",
            title="装配核对",
            started_at=started_at,
        ),
        mode=InvestigationMode.MULTI_AGENT,
        data_source_mode=InvestigationDataSourceMode.LIVE
        if live
        else InvestigationDataSourceMode.FROZEN,
        work_item_kind=WorkItemKind.RUN_MULTI_AGENT_INVESTIGATION,
        workflow_version=WorkflowVersion.V3,
        graph_version=LIVE_DYNAMIC_INVESTIGATION_GRAPH_VERSION
        if live
        else DYNAMIC_INVESTIGATION_GRAPH_VERSION,
        graph_thread_id=build_graph_thread_id(run_id),
        coordination_policy=CoordinationPolicy(),
        investigation_run_id=run_id,
        lease_token=uuid4(),
        call_budget=CallBudgetPolicy(),
        model_budget=policy,
        context_policy_version="summary-context-v1",
        evidence_policy_version="task-evidence-live-dynamic-mcp-v1" if live else "task-evidence-v1",
        observation_cutoff_at=started_at if live else None,
    )


def factory(context, *, source=None, executor=None):
    sessions = MagicMock(side_effect=AssertionError("装配不能访问数据库"))
    model = SimpleNamespace(model_name=context.model_budget.profile.model_name, max_retries=0, n=1)
    saver = InMemorySaver()
    return (
        CurrentDynamicGraphFactory(
            session_factory=sessions,
            model=model,
            checkpointer=saver,
            clock=lambda: context.request.started_at,
            data_source=source,
            query_executor=executor,
        ),
        sessions,
        saver,
    )


@pytest.mark.parametrize("live", [False, True])
def test_current_factory_builds_scoped_graph_without_external_work(live):
    run = context(live=live)
    logs, metrics, executor = MagicMock(), MagicMock(), MagicMock()
    source = LiveInvestigationDataSource(mcp_logs=logs, mcp_metrics=metrics)
    create, sessions, saver = factory(run, source=source, executor=executor)
    graph = create(run)

    assert graph._graph_version == run.graph_version
    assert graph._run_id == graph._runtime.run_id == run.investigation_run_id
    assert graph._runtime._lease_token == run.lease_token
    assert graph._runtime._call_runner is graph._analyst._runner
    assert graph._runtime._call_runner is graph._reviewer._runner
    assert graph._runtime._call_runner is graph._third_round_reviewer._runner
    assert graph._workflow.checkpointer is saver
    assert not saver.storage
    if live:
        assert isinstance(graph._live_source, DynamicLiveDataSource)
        assert graph._live_source.snapshot_id == live_snapshot_id(
            run.investigation_run_id, run.observation_cutoff_at
        )
        assert graph._live_source._executor is executor
        assert graph._data_source is None
    else:
        assert graph._live_source is None
        assert graph._data_source is not None
    sessions.assert_not_called()
    assert not logs.mock_calls and not metrics.mock_calls and not executor.mock_calls


@pytest.mark.parametrize("missing", ["source", "executor"])
def test_live_factory_rejects_missing_adapters_without_frozen_fallback(missing):
    run = context(live=True)
    source = LiveInvestigationDataSource()
    create, sessions, saver = factory(
        run,
        source=None if missing == "source" else source,
        executor=None if missing == "executor" else MagicMock(),
    )
    with pytest.raises(ValueError, match="未注入MCP"):
        create(run)
    sessions.assert_not_called()
    assert not saver.storage


def test_current_factory_rejects_legacy_graph_before_building():
    run = context(live=False).model_copy(
        update={"graph_version": "dynamic-investigation-graph-v14"}
    )
    create, sessions, saver = factory(run)
    with pytest.raises(ValueError, match="当前图版本"):
        create(run)
    sessions.assert_not_called()
    assert not saver.storage
