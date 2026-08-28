"""从持久真实模型尝试派生三场景批量对照报告。"""

from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from statistics import fmean, pstdev

from pydantic import Field, model_validator

from opsswarm.evaluation.attempts import (
    EvaluationAttemptBudget,
    EvaluationAttemptStatus,
    EvaluationFailureCategory,
    StoredEvaluationAttempt,
)
from opsswarm.evaluation.errors import EvaluationBudgetExceeded
from opsswarm.evaluation.metadata import EvaluationFailureCounts
from opsswarm.evaluation.models import (
    InvestigationEvaluationDataset,
    StrictEvaluationModel,
)
from opsswarm.evaluation.real_model import RealModelSmokeRunner
from opsswarm.investigation.models import InvestigationMode
from opsswarm.investigation.scenarios import InvestigationScenario

UtcClock = Callable[[], datetime]


def utc_now() -> datetime:
    """返回带时区的 UTC 当前时间。"""

    return datetime.now(UTC)


class RealModelAttemptDigest(StrictEvaluationModel):
    """批量报告中的紧凑尝试明细，不复制提示或供应商错误正文。"""

    sequence: int = Field(ge=1)
    repetition_index: int = Field(ge=1, le=1000)
    scenario_id: str
    mode: InvestigationMode
    status: EvaluationAttemptStatus
    top1_accuracy: float | None = Field(default=None, ge=0, le=1)
    evidence_recall: float | None = Field(default=None, ge=0, le=1)
    recommendation_accuracy: float | None = Field(default=None, ge=0, le=1)
    safety_violation_rate: float | None = Field(default=None, ge=0, le=1)
    model_request_count: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    estimated_cost_usd: Decimal = Field(ge=0, max_digits=12, decimal_places=6)
    duration_ms: float = Field(ge=0, allow_inf_nan=False)
    failure_category: EvaluationFailureCategory | None = None

    @model_validator(mode="after")
    def validate_status_shape(self) -> "RealModelAttemptDigest":
        """成功明细必须有质量指标，失败明细只能带失败分类。"""

        quality = (
            self.top1_accuracy,
            self.evidence_recall,
            self.recommendation_accuracy,
            self.safety_violation_rate,
        )
        if self.status is EvaluationAttemptStatus.SUCCEEDED:
            if any(item is None for item in quality) or self.failure_category is not None:
                raise ValueError("成功尝试摘要必须包含质量指标且不能包含失败分类")
        elif self.status is EvaluationAttemptStatus.FAILED:
            if any(item is not None for item in quality) or self.failure_category is None:
                raise ValueError("失败尝试摘要必须只包含失败分类")
        else:
            raise ValueError("批量报告不能包含仍在运行的尝试")
        return self


class RealModelModeAggregate(StrictEvaluationModel):
    """一种 Agent 模式跨场景、跨重复轮次的可靠性与成本统计。"""

    mode: InvestigationMode
    attempt_count: int = Field(ge=1)
    succeeded_count: int = Field(ge=0)
    failed_count: int = Field(ge=0)
    success_rate: float = Field(ge=0, le=1)
    top1_accuracy_including_failures: float = Field(ge=0, le=1)
    top1_accuracy_on_success: float | None = Field(default=None, ge=0, le=1)
    evidence_recall_on_success: float | None = Field(default=None, ge=0, le=1)
    recommendation_accuracy_on_success: float | None = Field(default=None, ge=0, le=1)
    safety_violation_rate_on_success: float | None = Field(default=None, ge=0, le=1)
    total_model_requests: int = Field(ge=0)
    mean_model_requests: float = Field(ge=0, allow_inf_nan=False)
    total_tokens: int = Field(ge=0)
    mean_tokens: float = Field(ge=0, allow_inf_nan=False)
    total_estimated_cost_usd: Decimal = Field(ge=0, max_digits=12, decimal_places=6)
    mean_estimated_cost_usd: Decimal = Field(ge=0, max_digits=12, decimal_places=6)
    estimated_cost_stddev_usd: Decimal = Field(ge=0, max_digits=12, decimal_places=6)
    mean_duration_ms: float = Field(ge=0, allow_inf_nan=False)
    duration_stddev_ms: float = Field(ge=0, allow_inf_nan=False)
    failures: EvaluationFailureCounts

    @model_validator(mode="after")
    def validate_counts(self) -> "RealModelModeAggregate":
        if self.succeeded_count + self.failed_count != self.attempt_count:
            raise ValueError("成功与失败数量必须覆盖全部尝试")
        if self.failures.total != self.failed_count:
            raise ValueError("失败分类合计必须等于失败尝试数量")
        return self


class RealModelAggregateDelta(StrictEvaluationModel):
    """多 Agent 减去单 Agent 的关键宏观差值。"""

    success_rate: float = Field(ge=-1, le=1)
    top1_accuracy_including_failures: float = Field(ge=-1, le=1)
    mean_model_requests: float = Field(allow_inf_nan=False)
    mean_tokens: float = Field(allow_inf_nan=False)
    mean_estimated_cost_usd: Decimal = Field(max_digits=12, decimal_places=6)
    mean_duration_ms: float = Field(allow_inf_nan=False)


class RealModelBatchReport(StrictEvaluationModel):
    """由不可变尝试事实派生的完整批量对照报告。"""

    dataset_key: str
    dataset_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    experiment_key: str
    repetition_indexes: tuple[int, ...] = Field(min_length=1)
    scenario_ids: tuple[str, ...] = Field(min_length=1)
    generated_at: datetime
    attempts: tuple[RealModelAttemptDigest, ...] = Field(min_length=2)
    single_agent: RealModelModeAggregate
    multi_agent: RealModelModeAggregate
    delta: RealModelAggregateDelta

    @model_validator(mode="after")
    def validate_complete_matrix(self) -> "RealModelBatchReport":
        if len(set(self.repetition_indexes)) != len(self.repetition_indexes):
            raise ValueError("重复轮次不能重复")
        if len(set(self.scenario_ids)) != len(self.scenario_ids):
            raise ValueError("场景不能重复")
        expected = {
            (repetition, scenario_id, mode)
            for repetition in self.repetition_indexes
            for scenario_id in self.scenario_ids
            for mode in (InvestigationMode.SINGLE_AGENT, InvestigationMode.MULTI_AGENT)
        }
        actual = {(item.repetition_index, item.scenario_id, item.mode) for item in self.attempts}
        if actual != expected or len(self.attempts) != len(expected):
            raise ValueError("批量报告必须完整覆盖每轮、每场景和两种 Agent 模式")
        expected_mode_count = len(self.repetition_indexes) * len(self.scenario_ids)
        if (
            self.single_agent.attempt_count != expected_mode_count
            or self.multi_agent.attempt_count != expected_mode_count
        ):
            raise ValueError("模式聚合数量与批量矩阵不一致")
        return self


class RealModelBatchRunner:
    """按确定计划调用单场景运行器，并从持久尝试构建聚合报告。"""

    def __init__(
        self,
        *,
        smoke_runner: RealModelSmokeRunner,
        budget: EvaluationAttemptBudget,
        max_batch_cost_usd: Decimal,
        utc_clock: UtcClock = utc_now,
    ) -> None:
        if max_batch_cost_usd <= 0:
            raise ValueError("批次费用上限必须大于 0")
        self._smoke_runner = smoke_runner
        self._budget = budget
        self._max_batch_cost_usd = max_batch_cost_usd
        self._utc_clock = utc_clock

    async def run(
        self,
        dataset: InvestigationEvaluationDataset,
        *,
        experiment_key: str,
        repetition_indexes: Sequence[int],
        scenario_ids: Sequence[str],
        idempotency_prefix: str,
    ) -> RealModelBatchReport:
        """公平交替执行全部计划；相同身份由底层尝试幂等复用。"""

        repetitions = tuple(repetition_indexes)
        selected_ids = tuple(scenario_ids)
        scenarios = self._validate_plan(dataset, repetitions, selected_ids, idempotency_prefix)
        planned_attempts = len(repetitions) * len(scenarios) * 2
        output_ceiling = self._maximum_output_cost_per_attempt() * planned_attempts
        if output_ceiling > self._max_batch_cost_usd:
            raise EvaluationBudgetExceeded("批量计划的最大输出 Token 费用超过批次预算")

        attempts: list[StoredEvaluationAttempt] = []
        for repetition in repetitions:
            for position, scenario in enumerate(scenarios):
                pair = await self._smoke_runner.run(
                    dataset,
                    scenario_id=scenario.scenario_id,
                    experiment_key=experiment_key,
                    repetition_index=repetition,
                    idempotency_prefix=(
                        f"{idempotency_prefix}:r{repetition}:{scenario.scenario_id}"
                    ),
                    single_agent_first=(repetition + position) % 2 == 1,
                )
                attempts.extend(pair)
                spent = sum(
                    (item.telemetry.estimated_cost_usd for item in attempts),
                    start=Decimal("0"),
                )
                if spent > self._max_batch_cost_usd:
                    raise EvaluationBudgetExceeded("批量累计估算费用超过批次预算")

        return build_real_model_batch_report(
            dataset,
            experiment_key=experiment_key,
            repetition_indexes=repetitions,
            scenario_ids=selected_ids,
            attempts=attempts,
            generated_at=self._utc_clock(),
        )

    def _validate_plan(
        self,
        dataset: InvestigationEvaluationDataset,
        repetitions: tuple[int, ...],
        scenario_ids: tuple[str, ...],
        idempotency_prefix: str,
    ) -> tuple[InvestigationScenario, ...]:
        if not repetitions or len(set(repetitions)) != len(repetitions):
            raise ValueError("批量评测必须包含互不重复的轮次")
        if any(not 1 <= item <= 1000 for item in repetitions):
            raise ValueError("重复轮次必须在 1 到 1000 之间")
        if not scenario_ids or len(set(scenario_ids)) != len(scenario_ids):
            raise ValueError("批量评测必须包含互不重复的场景")
        scenario_map = {item.scenario_id: item for item in dataset.scenarios}
        unknown = set(scenario_ids) - scenario_map.keys()
        if unknown:
            raise ValueError(f"批量评测包含未知场景：{sorted(unknown)}")
        for repetition in repetitions:
            for scenario_id in scenario_ids:
                longest_key = f"{idempotency_prefix}:r{repetition}:{scenario_id}:multi_agent"
                if len(longest_key) > 200:
                    raise ValueError("批量幂等键超过 200 个字符")
        return tuple(scenario_map[item] for item in scenario_ids)

    def _maximum_output_cost_per_attempt(self) -> Decimal:
        return (
            Decimal(self._budget.max_model_requests)
            * Decimal(self._budget.max_output_tokens_per_request)
            * self._budget.output_cost_per_million_usd
            / Decimal(1_000_000)
        )


def build_real_model_batch_report(
    dataset: InvestigationEvaluationDataset,
    *,
    experiment_key: str,
    repetition_indexes: Sequence[int],
    scenario_ids: Sequence[str],
    attempts: Sequence[StoredEvaluationAttempt],
    generated_at: datetime,
) -> RealModelBatchReport:
    """把终态尝试转换为失败计零分、成功另算质量的可审计报告。"""

    repetitions = tuple(repetition_indexes)
    selected_ids = tuple(scenario_ids)
    digests = tuple(_attempt_digest(item) for item in attempts)
    single = _aggregate_mode(digests, InvestigationMode.SINGLE_AGENT)
    multi = _aggregate_mode(digests, InvestigationMode.MULTI_AGENT)
    return RealModelBatchReport(
        dataset_key=dataset.dataset_key,
        dataset_content_hash=dataset.content_hash(),
        experiment_key=experiment_key,
        repetition_indexes=repetitions,
        scenario_ids=selected_ids,
        generated_at=generated_at,
        attempts=digests,
        single_agent=single,
        multi_agent=multi,
        delta=RealModelAggregateDelta(
            success_rate=_round_metric(multi.success_rate - single.success_rate),
            top1_accuracy_including_failures=_round_metric(
                multi.top1_accuracy_including_failures - single.top1_accuracy_including_failures
            ),
            mean_model_requests=_round_metric(
                multi.mean_model_requests - single.mean_model_requests
            ),
            mean_tokens=_round_metric(multi.mean_tokens - single.mean_tokens),
            mean_estimated_cost_usd=_quantize_cost(
                multi.mean_estimated_cost_usd - single.mean_estimated_cost_usd
            ),
            mean_duration_ms=round(multi.mean_duration_ms - single.mean_duration_ms, 3),
        ),
    )


def _attempt_digest(attempt: StoredEvaluationAttempt) -> RealModelAttemptDigest:
    if attempt.status is EvaluationAttemptStatus.RUNNING or attempt.finished_at is None:
        raise ValueError("批量报告不能包含仍在运行的尝试")
    result = attempt.result
    duration_ms = (
        result.duration_ms
        if result is not None
        else (attempt.finished_at - attempt.started_at).total_seconds() * 1000
    )
    return RealModelAttemptDigest(
        sequence=attempt.sequence,
        repetition_index=attempt.repetition_index,
        scenario_id=attempt.scenario_id,
        mode=attempt.mode,
        status=attempt.status,
        top1_accuracy=result.metrics.top1_accuracy if result is not None else None,
        evidence_recall=result.metrics.evidence_recall if result is not None else None,
        recommendation_accuracy=(
            result.metrics.recommendation_accuracy if result is not None else None
        ),
        safety_violation_rate=(
            result.metrics.safety_violation_rate if result is not None else None
        ),
        model_request_count=attempt.telemetry.model_request_count,
        total_tokens=attempt.telemetry.total_tokens,
        estimated_cost_usd=attempt.telemetry.estimated_cost_usd,
        duration_ms=round(duration_ms, 3),
        failure_category=attempt.failure_category,
    )


def _aggregate_mode(
    attempts: Sequence[RealModelAttemptDigest],
    mode: InvestigationMode,
) -> RealModelModeAggregate:
    selected = tuple(item for item in attempts if item.mode is mode)
    if not selected:
        raise ValueError(f"缺少 {mode.value} 尝试")
    succeeded = tuple(item for item in selected if item.status is EvaluationAttemptStatus.SUCCEEDED)
    failures = Counter(
        item.failure_category for item in selected if item.failure_category is not None
    )
    costs = tuple(item.estimated_cost_usd for item in selected)
    durations = tuple(item.duration_ms for item in selected)
    return RealModelModeAggregate(
        mode=mode,
        attempt_count=len(selected),
        succeeded_count=len(succeeded),
        failed_count=len(selected) - len(succeeded),
        success_rate=_round_metric(len(succeeded) / len(selected)),
        top1_accuracy_including_failures=_round_metric(
            sum(item.top1_accuracy or 0 for item in selected) / len(selected)
        ),
        top1_accuracy_on_success=_optional_mean(item.top1_accuracy for item in succeeded),
        evidence_recall_on_success=_optional_mean(item.evidence_recall for item in succeeded),
        recommendation_accuracy_on_success=_optional_mean(
            item.recommendation_accuracy for item in succeeded
        ),
        safety_violation_rate_on_success=_optional_mean(
            item.safety_violation_rate for item in succeeded
        ),
        total_model_requests=sum(item.model_request_count for item in selected),
        mean_model_requests=_round_metric(fmean(item.model_request_count for item in selected)),
        total_tokens=sum(item.total_tokens for item in selected),
        mean_tokens=_round_metric(fmean(item.total_tokens for item in selected)),
        total_estimated_cost_usd=_quantize_cost(sum(costs, start=Decimal("0"))),
        mean_estimated_cost_usd=_quantize_cost(
            sum(costs, start=Decimal("0")) / Decimal(len(costs))
        ),
        estimated_cost_stddev_usd=_decimal_pstdev(costs),
        mean_duration_ms=round(fmean(durations), 3),
        duration_stddev_ms=round(pstdev(durations), 3),
        failures=EvaluationFailureCounts(
            **{category.value: failures[category] for category in EvaluationFailureCategory}
        ),
    )


def _optional_mean(values: Iterable[float | None]) -> float | None:
    normalized = tuple(item for item in values if item is not None)
    return _round_metric(fmean(normalized)) if normalized else None


def _decimal_pstdev(values: Sequence[Decimal]) -> Decimal:
    if len(values) <= 1:
        return Decimal("0.000000")
    mean = sum(values, start=Decimal("0")) / Decimal(len(values))
    variance = sum(((item - mean) ** 2 for item in values), start=Decimal("0")) / Decimal(
        len(values)
    )
    return _quantize_cost(variance.sqrt())


def _quantize_cost(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)


def _round_metric(value: float) -> float:
    return round(value, 6)
