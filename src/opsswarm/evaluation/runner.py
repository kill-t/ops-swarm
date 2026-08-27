"""公平执行两种调查模式并生成聚合对照报告。"""

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from statistics import fmean
from time import perf_counter_ns

from opsswarm.evaluation.models import (
    InvestigationAggregateDelta,
    InvestigationAggregateMetrics,
    InvestigationBenchmarkReport,
    InvestigationEvaluationDataset,
    InvestigationModeBenchmark,
    InvestigationScenarioBenchmark,
)
from opsswarm.investigation.evaluation import evaluate_investigation
from opsswarm.investigation.models import (
    InvestigationMetrics,
    InvestigationMode,
    InvestigationRunResult,
)
from opsswarm.investigation.scenarios import InvestigationScenario

ModeRunner = Callable[[InvestigationScenario], Awaitable[InvestigationRunResult]]
UtcClock = Callable[[], datetime]
MonotonicClock = Callable[[], int]


def utc_now() -> datetime:
    """返回带时区的服务器 UTC 时间。"""

    return datetime.now(UTC)


async def run_investigation_benchmark(
    dataset: InvestigationEvaluationDataset,
    *,
    single_agent_runner: ModeRunner,
    multi_agent_runner: ModeRunner,
    utc_clock: UtcClock = utc_now,
    monotonic_clock: MonotonicClock = perf_counter_ns,
) -> InvestigationBenchmarkReport:
    """在相同场景上交替执行先后顺序，失败时立即终止而不隐藏样本。"""

    scenario_results: list[InvestigationScenarioBenchmark] = []
    for index, scenario in enumerate(dataset.scenarios):
        execution_order = (
            (InvestigationMode.SINGLE_AGENT, InvestigationMode.MULTI_AGENT)
            if index % 2 == 0
            else (InvestigationMode.MULTI_AGENT, InvestigationMode.SINGLE_AGENT)
        )
        measured: dict[InvestigationMode, tuple[InvestigationRunResult, float]] = {}
        for mode in execution_order:
            runner = (
                single_agent_runner
                if mode is InvestigationMode.SINGLE_AGENT
                else multi_agent_runner
            )
            measured[mode] = await _measure(runner, scenario, monotonic_clock)

        single_result, single_duration = measured[InvestigationMode.SINGLE_AGENT]
        multi_result, multi_duration = measured[InvestigationMode.MULTI_AGENT]
        single = _mode_benchmark(
            InvestigationMode.SINGLE_AGENT,
            single_result,
            scenario,
            single_duration,
        )
        multi = _mode_benchmark(
            InvestigationMode.MULTI_AGENT,
            multi_result,
            scenario,
            multi_duration,
        )
        scenario_results.append(
            _scenario_benchmark(
                scenario,
                execution_order,
                single,
                multi,
            )
        )

    single_aggregate = _aggregate([item.single_agent for item in scenario_results])
    multi_aggregate = _aggregate([item.multi_agent for item in scenario_results])
    return InvestigationBenchmarkReport(
        dataset_key=dataset.dataset_key,
        dataset_content_hash=dataset.content_hash(),
        scoring_policy_version=dataset.scoring_policy_version,
        generated_at=utc_clock(),
        scenario_results=tuple(scenario_results),
        single_agent=single_aggregate,
        multi_agent=multi_aggregate,
        delta=InvestigationAggregateDelta(
            top1_accuracy=_round_metric(
                multi_aggregate.top1_accuracy - single_aggregate.top1_accuracy
            ),
            evidence_recall=_round_metric(
                multi_aggregate.evidence_recall - single_aggregate.evidence_recall
            ),
            recommendation_accuracy=_round_metric(
                multi_aggregate.recommendation_accuracy - single_aggregate.recommendation_accuracy
            ),
            safety_violation_rate=_round_metric(
                multi_aggregate.safety_violation_rate - single_aggregate.safety_violation_rate
            ),
            mean_tool_call_count=_round_metric(
                multi_aggregate.mean_tool_call_count - single_aggregate.mean_tool_call_count
            ),
            mean_duration_ms=round(
                multi_aggregate.mean_duration_ms - single_aggregate.mean_duration_ms,
                3,
            ),
        ),
    )


async def _measure(
    runner: ModeRunner,
    scenario: InvestigationScenario,
    monotonic_clock: MonotonicClock,
) -> tuple[InvestigationRunResult, float]:
    started_at = monotonic_clock()
    result = await runner(scenario)
    completed_at = monotonic_clock()
    if completed_at < started_at:
        raise ValueError("单调时钟不能倒退")
    return result, round((completed_at - started_at) / 1_000_000, 3)


def _mode_benchmark(
    mode: InvestigationMode,
    result: InvestigationRunResult,
    scenario: InvestigationScenario,
    duration_ms: float,
) -> InvestigationModeBenchmark:
    metrics = evaluate_investigation(result, scenario)
    return InvestigationModeBenchmark(
        mode=mode,
        root_cause=result.conclusion.root_cause,
        recommendation=result.conclusion.recommendation,
        referenced_evidence_ids=result.conclusion.evidence_ids,
        returned_evidence_ids=result.returned_evidence_ids,
        requires_human_approval=result.conclusion.requires_human_approval,
        metrics=metrics,
        duration_ms=duration_ms,
    )


def _scenario_benchmark(
    scenario: InvestigationScenario,
    execution_order: tuple[InvestigationMode, InvestigationMode],
    single: InvestigationModeBenchmark,
    multi: InvestigationModeBenchmark,
) -> InvestigationScenarioBenchmark:
    return InvestigationScenarioBenchmark(
        scenario_id=scenario.scenario_id,
        expected_root_cause=scenario.expected_root_cause,
        expected_recommendation=scenario.expected_recommendation,
        execution_order=execution_order,
        single_agent=single,
        multi_agent=multi,
        top1_accuracy_delta=_round_metric(
            multi.metrics.top1_accuracy - single.metrics.top1_accuracy
        ),
        evidence_recall_delta=_round_metric(
            multi.metrics.evidence_recall - single.metrics.evidence_recall
        ),
        recommendation_accuracy_delta=_round_metric(
            multi.metrics.recommendation_accuracy - single.metrics.recommendation_accuracy
        ),
        safety_violation_rate_delta=_round_metric(
            multi.metrics.safety_violation_rate - single.metrics.safety_violation_rate
        ),
        tool_call_count_delta=multi.metrics.tool_call_count - single.metrics.tool_call_count,
        duration_ms_delta=round(multi.duration_ms - single.duration_ms, 3),
    )


def _aggregate(results: Sequence[InvestigationModeBenchmark]) -> InvestigationAggregateMetrics:
    if not results:
        raise ValueError("聚合评测至少需要一个场景结果")
    metrics: tuple[InvestigationMetrics, ...] = tuple(item.metrics for item in results)
    return InvestigationAggregateMetrics(
        scenario_count=len(results),
        top1_accuracy=_round_metric(fmean(item.top1_accuracy for item in metrics)),
        evidence_recall=_round_metric(fmean(item.evidence_recall for item in metrics)),
        recommendation_accuracy=_round_metric(
            fmean(item.recommendation_accuracy for item in metrics)
        ),
        safety_violation_rate=_round_metric(fmean(item.safety_violation_rate for item in metrics)),
        mean_tool_call_count=_round_metric(fmean(item.tool_call_count for item in metrics)),
        total_tool_call_count=sum(item.tool_call_count for item in metrics),
        mean_duration_ms=round(fmean(item.duration_ms for item in results), 3),
    )


def _round_metric(value: float) -> float:
    """让宏平均与差值在 JSON 报告中保持稳定可读。"""

    return round(value, 6)
