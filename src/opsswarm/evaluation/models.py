"""版本化调查评测集与聚合报告模型。"""

import hashlib
import json
from datetime import UTC, datetime
from enum import Enum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opsswarm.investigation.models import (
    InvestigationMetrics,
    InvestigationMode,
    Recommendation,
    RootCause,
)
from opsswarm.investigation.scenarios import InvestigationScenario


class StrictEvaluationModel(BaseModel):
    """评测模型拒绝额外字段并保持不可变。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class InvestigationEvaluationDataset(StrictEvaluationModel):
    """把冻结场景、数据版本和评分规则绑定为一份评测集。"""

    dataset_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9-]*$",
    )
    version: int = Field(ge=1)
    scoring_policy_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    scenarios: tuple[InvestigationScenario, ...] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def validate_scenario_identity(self) -> Self:
        """防止同一数据集重复计分或混入未显式升级的场景。"""

        scenario_ids = [item.scenario_id for item in self.scenarios]
        if len(set(scenario_ids)) != len(scenario_ids):
            raise ValueError("评测集中的 scenario_id 不能重复")
        incident_ids = [item.request.incident_id for item in self.scenarios]
        if len(set(incident_ids)) != len(incident_ids):
            raise ValueError("评测集中的 incident_id 不能重复")
        expected_suffix = f"-v{self.version}"
        if any(not item.scenario_id.endswith(expected_suffix) for item in self.scenarios):
            raise ValueError("场景版本必须与评测集版本一致")
        return self

    @property
    def dataset_key(self) -> str:
        """返回适合报告和审计关联的稳定版本标识。"""

        return f"{self.dataset_id}-v{self.version}"

    def content_hash(self) -> str:
        """对规范化完整内容计算 SHA-256，检测同版本静默改写。"""

        payload = json.dumps(
            self.canonical_payload(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def canonical_payload(self) -> dict[str, object]:
        """返回可稳定序列化的数据集快照，供哈希和持久化共用。"""

        payload = _canonicalize(self)
        if not isinstance(payload, dict):
            raise TypeError("评测集规范化结果必须是对象")
        return payload


class InvestigationModeBenchmark(StrictEvaluationModel):
    """一个模式在一个场景上的可审查输出与指标。"""

    mode: InvestigationMode
    root_cause: RootCause
    recommendation: Recommendation
    referenced_evidence_ids: tuple[str, ...]
    returned_evidence_ids: tuple[str, ...]
    requires_human_approval: bool
    metrics: InvestigationMetrics
    duration_ms: float = Field(ge=0, allow_inf_nan=False)


class InvestigationScenarioBenchmark(StrictEvaluationModel):
    """同一冻结场景下两种调查模式的逐项对照。"""

    scenario_id: str
    expected_root_cause: RootCause
    expected_recommendation: Recommendation
    execution_order: tuple[InvestigationMode, InvestigationMode]
    single_agent: InvestigationModeBenchmark
    multi_agent: InvestigationModeBenchmark
    top1_accuracy_delta: float = Field(ge=-1, le=1)
    evidence_recall_delta: float = Field(ge=-1, le=1)
    recommendation_accuracy_delta: float = Field(ge=-1, le=1)
    safety_violation_rate_delta: float = Field(ge=-1, le=1)
    tool_call_count_delta: int
    duration_ms_delta: float = Field(allow_inf_nan=False)

    @model_validator(mode="after")
    def validate_modes(self) -> Self:
        """报告必须恰好包含一次单 Agent 和一次多 Agent。"""

        expected_modes = {InvestigationMode.SINGLE_AGENT, InvestigationMode.MULTI_AGENT}
        if set(self.execution_order) != expected_modes:
            raise ValueError("execution_order 必须同时包含单 Agent 和多 Agent")
        if self.single_agent.mode is not InvestigationMode.SINGLE_AGENT:
            raise ValueError("single_agent 结果的模式不正确")
        if self.multi_agent.mode is not InvestigationMode.MULTI_AGENT:
            raise ValueError("multi_agent 结果的模式不正确")
        return self


def _canonicalize(value: object) -> object:
    """保留有序序列顺序，同时稳定排序集合和映射。"""

    if isinstance(value, BaseModel):
        return _canonicalize(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {str(key): _canonicalize(item) for key, item in value.items()}
    if isinstance(value, set | frozenset):
        canonical_items = [_canonicalize(item) for item in value]
        return sorted(
            canonical_items,
            key=lambda item: json.dumps(
                item,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ),
        )
    if isinstance(value, list | tuple):
        return [_canonicalize(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if isinstance(value, UUID):
        return str(value)
    return value


class InvestigationAggregateMetrics(StrictEvaluationModel):
    """一种模式在整份数据集上的宏平均指标。"""

    scenario_count: int = Field(ge=1)
    top1_accuracy: float = Field(ge=0, le=1)
    evidence_recall: float = Field(ge=0, le=1)
    recommendation_accuracy: float = Field(ge=0, le=1)
    safety_violation_rate: float = Field(ge=0, le=1)
    mean_tool_call_count: float = Field(ge=0, allow_inf_nan=False)
    total_tool_call_count: int = Field(ge=0)
    mean_duration_ms: float = Field(ge=0, allow_inf_nan=False)


class InvestigationAggregateDelta(StrictEvaluationModel):
    """宏平均意义下多 Agent 减去单 Agent 的差值。"""

    top1_accuracy: float = Field(ge=-1, le=1)
    evidence_recall: float = Field(ge=-1, le=1)
    recommendation_accuracy: float = Field(ge=-1, le=1)
    safety_violation_rate: float = Field(ge=-1, le=1)
    mean_tool_call_count: float = Field(allow_inf_nan=False)
    mean_duration_ms: float = Field(allow_inf_nan=False)


class InvestigationBenchmarkReport(StrictEvaluationModel):
    """一轮可复现的单 Agent、多 Agent 数据集对照报告。"""

    dataset_key: str
    dataset_content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    scoring_policy_version: str
    generated_at: datetime
    scenario_results: tuple[InvestigationScenarioBenchmark, ...] = Field(min_length=1)
    single_agent: InvestigationAggregateMetrics
    multi_agent: InvestigationAggregateMetrics
    delta: InvestigationAggregateDelta

    @field_validator("generated_at")
    @classmethod
    def normalize_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at 必须包含时区")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_scenario_count(self) -> Self:
        count = len(self.scenario_results)
        if self.single_agent.scenario_count != count or self.multi_agent.scenario_count != count:
            raise ValueError("聚合场景数量必须与逐场景结果一致")
        return self
