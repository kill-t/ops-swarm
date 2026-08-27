from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from opsswarm.evaluation import (
    MVP_INVESTIGATION_DATASET_V1,
    run_investigation_benchmark,
)
from opsswarm.evaluation.fixtures import run_fixture_benchmark
from opsswarm.investigation.models import (
    InvestigationConclusion,
    InvestigationMode,
    InvestigationRunResult,
    RootCause,
)
from opsswarm.investigation.scenarios import InvestigationScenario

FIXED_REPORT_TIME = datetime(2026, 1, 4, 8, tzinfo=UTC)


@pytest.mark.asyncio
async def test_fixture_benchmark_runs_three_scenarios_through_both_frameworks() -> None:
    report = await run_fixture_benchmark()

    assert report.dataset_key == MVP_INVESTIGATION_DATASET_V1.dataset_key
    assert report.dataset_content_hash == MVP_INVESTIGATION_DATASET_V1.content_hash()
    assert len(report.scenario_results) == 3
    assert report.single_agent.top1_accuracy == 1
    assert report.multi_agent.top1_accuracy == 1
    assert report.single_agent.evidence_recall == 1
    assert report.multi_agent.evidence_recall == 1
    assert report.single_agent.total_tool_call_count == 9
    assert report.multi_agent.total_tool_call_count == 9
    assert report.delta.top1_accuracy == 0
    assert report.delta.evidence_recall == 0
    assert report.delta.mean_tool_call_count == 0
    assert [item.execution_order for item in report.scenario_results] == [
        (InvestigationMode.SINGLE_AGENT, InvestigationMode.MULTI_AGENT),
        (InvestigationMode.MULTI_AGENT, InvestigationMode.SINGLE_AGENT),
        (InvestigationMode.SINGLE_AGENT, InvestigationMode.MULTI_AGENT),
    ]


@pytest.mark.asyncio
async def test_aggregate_report_does_not_hide_single_agent_quality_gap() -> None:
    ticks: Iterator[int] = iter(range(0, 20_000_000, 1_000_000))

    async def single_runner(scenario: InvestigationScenario) -> InvestigationRunResult:
        if scenario.expected_root_cause is RootCause.DATABASE_POOL_MISCONFIGURATION:
            return _result(
                scenario,
                root_cause=RootCause.UNDETERMINED,
                evidence_ids=(sorted(scenario.required_evidence_ids)[0],),
                tool_call_count=2,
            )
        return _result(scenario, tool_call_count=2)

    async def multi_runner(scenario: InvestigationScenario) -> InvestigationRunResult:
        return _result(scenario, tool_call_count=3)

    report = await run_investigation_benchmark(
        MVP_INVESTIGATION_DATASET_V1,
        single_agent_runner=single_runner,
        multi_agent_runner=multi_runner,
        utc_clock=lambda: FIXED_REPORT_TIME,
        monotonic_clock=lambda: next(ticks),
    )

    assert report.generated_at == FIXED_REPORT_TIME
    assert report.single_agent.top1_accuracy == 0.666667
    assert report.multi_agent.top1_accuracy == 1
    assert report.delta.top1_accuracy == 0.333333
    assert report.single_agent.evidence_recall == 0.833333
    assert report.delta.evidence_recall == 0.166667
    assert report.delta.mean_tool_call_count == 1
    assert report.single_agent.mean_duration_ms == 1
    assert report.multi_agent.mean_duration_ms == 1


@pytest.mark.asyncio
async def test_benchmark_rejects_a_monotonic_clock_that_moves_backwards() -> None:
    ticks = iter((2_000_000, 1_000_000))

    async def runner(selected: InvestigationScenario) -> InvestigationRunResult:
        return _result(selected, tool_call_count=3)

    with pytest.raises(ValueError, match="单调时钟不能倒退"):
        await run_investigation_benchmark(
            MVP_INVESTIGATION_DATASET_V1,
            single_agent_runner=runner,
            multi_agent_runner=runner,
            monotonic_clock=lambda: next(ticks),
        )


def _result(
    scenario: InvestigationScenario,
    *,
    root_cause: RootCause | None = None,
    evidence_ids: tuple[str, ...] | None = None,
    tool_call_count: int,
) -> InvestigationRunResult:
    selected_evidence_ids = evidence_ids or tuple(sorted(scenario.required_evidence_ids))
    return InvestigationRunResult(
        conclusion=InvestigationConclusion(
            root_cause=root_cause or scenario.expected_root_cause,
            confidence=0.9,
            summary="离线聚合评分测试结论。",
            evidence_ids=selected_evidence_ids,
            recommendation=scenario.expected_recommendation,
            requires_human_approval=(scenario.expected_recommendation.value == "rollback_release"),
        ),
        returned_evidence_ids=tuple(item.evidence_id for item in scenario.evidence),
        tool_call_count=tool_call_count,
    )
