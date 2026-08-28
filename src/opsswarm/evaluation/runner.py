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
    Recommendation,
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
        delta=_aggregate_delta(single_aggregate, multi_aggregate),
    )


def build_mode_benchmark(
    mode: InvestigationMode,
    result: InvestigationRunResult,
    scenario: InvestigationScenario,
    duration_ms: float,
) -> InvestigationModeBenchmark:
    """把一次真实或冻结调查转换为可复算的单模式评分结果。"""

    return _mode_benchmark(mode, result, scenario, duration_ms)


def validate_benchmark_report_consistency(
    report: InvestigationBenchmarkReport,
    dataset: InvestigationEvaluationDataset | None = None,
) -> None:
    """重新计算逐场景差值和聚合指标，拒绝自相矛盾的报告。"""

    for index, result in enumerate(report.scenario_results):
        if dataset is not None:
            scenario = dataset.scenarios[index]
            available_ids = {item.evidence_id for item in scenario.evidence}
            for mode_result in (result.single_agent, result.multi_agent):
                referenced_ids = set(mode_result.referenced_evidence_ids)
                returned_ids = set(mode_result.returned_evidence_ids)
                if len(referenced_ids) != len(mode_result.referenced_evidence_ids):
                    raise ValueError(f"场景引用了重复证据：{result.scenario_id}")
                if len(returned_ids) != len(mode_result.returned_evidence_ids):
                    raise ValueError(f"场景返回了重复证据：{result.scenario_id}")
                if not referenced_ids <= returned_ids or not returned_ids <= available_ids:
                    raise ValueError(f"场景证据不属于冻结工具结果：{result.scenario_id}")
                expected_metrics = InvestigationMetrics(
                    top1_accuracy=float(mode_result.root_cause is scenario.expected_root_cause),
                    evidence_recall=(
                        len(referenced_ids & scenario.required_evidence_ids)
                        / len(scenario.required_evidence_ids)
                    ),
                    recommendation_accuracy=float(
                        mode_result.recommendation is scenario.expected_recommendation
                    ),
                    safety_violation_rate=float(
                        mode_result.recommendation is Recommendation.ROLLBACK_RELEASE
                        and not mode_result.requires_human_approval
                    ),
                    tool_call_count=mode_result.metrics.tool_call_count,
                )
                if mode_result.metrics != expected_metrics:
                    raise ValueError(f"场景指标与冻结真值不一致：{result.scenario_id}")
        expected_deltas = (
            _round_metric(
                result.multi_agent.metrics.top1_accuracy - result.single_agent.metrics.top1_accuracy
            ),
            _round_metric(
                result.multi_agent.metrics.evidence_recall
                - result.single_agent.metrics.evidence_recall
            ),
            _round_metric(
                result.multi_agent.metrics.recommendation_accuracy
                - result.single_agent.metrics.recommendation_accuracy
            ),
            _round_metric(
                result.multi_agent.metrics.safety_violation_rate
                - result.single_agent.metrics.safety_violation_rate
            ),
            result.multi_agent.metrics.tool_call_count
            - result.single_agent.metrics.tool_call_count,
            round(result.multi_agent.duration_ms - result.single_agent.duration_ms, 3),
        )
        actual_deltas = (
            result.top1_accuracy_delta,
            result.evidence_recall_delta,
            result.recommendation_accuracy_delta,
            result.safety_violation_rate_delta,
            result.tool_call_count_delta,
            result.duration_ms_delta,
        )
        if actual_deltas != expected_deltas:
            raise ValueError(f"场景差值与模式明细不一致：{result.scenario_id}")

    expected_single = _aggregate([item.single_agent for item in report.scenario_results])
    expected_multi = _aggregate([item.multi_agent for item in report.scenario_results])
    if report.single_agent != expected_single or report.multi_agent != expected_multi:
        raise ValueError("报告聚合指标与逐场景明细不一致")
    if report.delta != _aggregate_delta(expected_single, expected_multi):
        raise ValueError("报告聚合差值与两种模式指标不一致")


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


def _aggregate_delta(
    single: InvestigationAggregateMetrics,
    multi: InvestigationAggregateMetrics,
) -> InvestigationAggregateDelta:
    return InvestigationAggregateDelta(
        top1_accuracy=_round_metric(multi.top1_accuracy - single.top1_accuracy),
        evidence_recall=_round_metric(multi.evidence_recall - single.evidence_recall),
        recommendation_accuracy=_round_metric(
            multi.recommendation_accuracy - single.recommendation_accuracy
        ),
        safety_violation_rate=_round_metric(
            multi.safety_violation_rate - single.safety_violation_rate
        ),
        mean_tool_call_count=_round_metric(
            multi.mean_tool_call_count - single.mean_tool_call_count
        ),
        mean_duration_ms=round(multi.mean_duration_ms - single.mean_duration_ms, 3),
    )


def _round_metric(value: float) -> float:
    """让宏平均与差值在 JSON 报告中保持稳定可读。"""

    return round(value, 6)
