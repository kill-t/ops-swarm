"""现场动态图与历史来源隔离、精确窗口及迁移元数据保持一致。"""

import importlib.util
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError

from opsswarm.control_plane.schemas import StartInvestigationRequest
from opsswarm.investigation.models import InvestigationDataSourceMode, InvestigationMode
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot, live_snapshot_id
from opsswarm.investigation.workflow_versions import (
    DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    LIVE_DYNAMIC_INVESTIGATION_GRAPH_VERSION,
    WorkflowVersion,
    is_supported_dynamic_source,
    validate_workflow_route,
)
from tests.loop_helpers import loop_policy
from tests.task_facts_helpers import BASE_TIME

POLICY = "task-evidence-live-dynamic-mcp-v1"


@pytest.mark.parametrize("version", range(1, 16))
def test_frozen_history_keeps_its_source_contract(version):
    graph = f"dynamic-investigation-graph-v{version}"
    assert is_supported_dynamic_source(graph, "frozen", "task-evidence-v1")
    assert not is_supported_dynamic_source(graph, "live", POLICY)


@pytest.mark.parametrize(
    "mode,policy",
    [
        ("frozen", "task-evidence-v1"),
        ("live", "task-evidence-live-mcp-v1"),
        ("live", None),
        ("unknown", POLICY),
    ],
)
def test_new_graph_rejects_crossed_source_contracts(mode, policy):
    assert not is_supported_dynamic_source(LIVE_DYNAMIC_INVESTIGATION_GRAPH_VERSION, mode, policy)


def test_live_v3_start_preserves_legacy_default_and_allows_explicit_experience():
    assert DYNAMIC_INVESTIGATION_GRAPH_VERSION == "dynamic-investigation-graph-v15"
    request = StartInvestigationRequest(
        expected_version=1,
        reason="现场调查",
        mode="multi_agent",
        workflow_version="v3",
        data_source_mode="live",
        model_budget=loop_policy(),
        coordination_experience_policy_version="coordination-experience-v1",
    )
    assert request.data_source_mode is InvestigationDataSourceMode.LIVE
    assert is_supported_dynamic_source(LIVE_DYNAMIC_INVESTIGATION_GRAPH_VERSION, "live", POLICY)
    with pytest.raises(ValueError, match="多 Agent"):
        validate_workflow_route(
            WorkflowVersion.V3, InvestigationMode.SINGLE_AGENT, InvestigationDataSourceMode.LIVE
        )


@pytest.mark.parametrize(
    "role,seconds,valid",
    [
        ("log", 60, True),
        ("metric", 900, True),
        ("change", 21600, True),
        ("log", 61, False),
        ("metric", 960, False),
        ("change", 21660, False),
        ("change", 3599, False),
        ("log", 0, False),
    ],
)
def test_dynamic_evidence_requires_bounded_complete_minutes(role, seconds, valid):
    body = dict(
        version=POLICY,
        data_source_mode="live",
        evidence_snapshot_id=live_snapshot_id(uuid4(), BASE_TIME),
        role=role,
        service_name="order-service",
        window_end=BASE_TIME,
        window_start=BASE_TIME - timedelta(seconds=seconds),
        evidence=(),
    )
    if valid:
        assert TaskEvidenceSnapshot(**body).version == POLICY
    else:
        with pytest.raises(ValidationError):
            TaskEvidenceSnapshot(**body)


def test_migration_and_metadata_share_the_same_new_constraints():
    path = (
        Path(__file__).resolve().parents[3] / "alembic/versions/0097_live_dynamic_investigation.py"
    )
    spec = importlib.util.spec_from_file_location("live_dynamic_migration", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    constraints = {
        item.name: str(item.sqltext)
        for item in InvestigationRunRow.__table__.constraints
        if hasattr(item, "sqltext")
    }
    assert all(
        constraints[name] == expression for name, expression in module._LIVE_CONSTRAINTS.items()
    )
    assert InvestigationRunRow.__table__.c.evidence_policy_version.type.length >= len(POLICY)
