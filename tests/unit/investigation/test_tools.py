import pytest
from pydantic import ValidationError

from opsswarm.investigation.errors import UnsupportedInvestigationTarget
from opsswarm.investigation.scenarios import (
    RELEASE_500_SCENARIO,
    FrozenScenarioDataSource,
)
from opsswarm.investigation.tools import EvidenceCollector, create_investigation_tools


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
