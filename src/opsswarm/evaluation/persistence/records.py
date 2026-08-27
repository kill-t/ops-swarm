"""评测报告持久化输入与只读模型。"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from opsswarm.evaluation.errors import EvaluationReportMismatch
from opsswarm.evaluation.metadata import EvaluationExperimentSpec, EvaluationRunTelemetry
from opsswarm.evaluation.models import (
    InvestigationAggregateDelta,
    InvestigationAggregateMetrics,
    InvestigationBenchmarkReport,
    InvestigationEvaluationDataset,
    StrictEvaluationModel,
)
from opsswarm.evaluation.runner import validate_benchmark_report_consistency


class EvaluationRunSubmission(StrictEvaluationModel):
    """离线评测命令提交给业务库的一次完整原子写入。"""

    dataset: InvestigationEvaluationDataset
    report: InvestigationBenchmarkReport
    idempotency_key: str = Field(
        min_length=8,
        max_length=200,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    runner_name: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$",
    )
    experiment: EvaluationExperimentSpec
    repetition_index: int = Field(ge=1, le=1000)
    telemetry: EvaluationRunTelemetry
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_created_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("评测持久化时间必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_report_binding(self) -> Self:
        """拒绝把报告挂到错误的数据集版本或真实标签。"""

        if self.report.dataset_key != self.dataset.dataset_key:
            raise EvaluationReportMismatch("报告的数据集标识与快照不一致")
        if self.report.dataset_content_hash != self.dataset.content_hash():
            raise EvaluationReportMismatch("报告的数据集哈希与快照不一致")
        if self.report.scoring_policy_version != self.dataset.scoring_policy_version:
            raise EvaluationReportMismatch("报告的评分规则版本与快照不一致")
        expected_scenarios = self.dataset.scenarios
        actual_results = self.report.scenario_results
        if tuple(item.scenario_id for item in actual_results) != tuple(
            item.scenario_id for item in expected_scenarios
        ):
            raise EvaluationReportMismatch("报告场景顺序与评测集不一致")
        if any(
            result.expected_root_cause is not scenario.expected_root_cause
            or result.expected_recommendation is not scenario.expected_recommendation
            for scenario, result in zip(expected_scenarios, actual_results, strict=True)
        ):
            raise EvaluationReportMismatch("报告中的场景真实标签与评测集不一致")
        try:
            validate_benchmark_report_consistency(self.report, self.dataset)
        except ValueError as error:
            raise EvaluationReportMismatch("报告指标无法由逐场景结果重算") from error
        return self


class StoredEvaluationRunSummary(StrictEvaluationModel):
    """评测运行列表所需的稳定概要。"""

    id: UUID
    sequence: int = Field(ge=1)
    dataset_key: str
    dataset_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scoring_policy_version: str
    scenario_count: int = Field(ge=1, le=100)
    runner_name: str
    experiment: EvaluationExperimentSpec
    repetition_index: int = Field(ge=1, le=1000)
    telemetry: EvaluationRunTelemetry
    generated_at: datetime
    created_at: datetime
    single_agent: InvestigationAggregateMetrics
    multi_agent: InvestigationAggregateMetrics
    delta: InvestigationAggregateDelta


class StoredEvaluationRun(StoredEvaluationRunSummary):
    """可审计的一次评测运行及其完整冻结报告。"""

    idempotency_key: str
    report_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    report: InvestigationBenchmarkReport


class EvaluationRunPage(StrictEvaluationModel):
    """按全局序号倒序读取的评测运行分页。"""

    items: tuple[StoredEvaluationRunSummary, ...]
    next_before_sequence: int | None = Field(default=None, ge=1)


@dataclass(frozen=True, slots=True)
class EvaluationRunCreation:
    """区分首次持久化和相同幂等键重放。"""

    run: StoredEvaluationRun
    created: bool
