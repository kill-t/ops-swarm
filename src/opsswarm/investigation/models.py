"""调查输入、证据与结构化结论。"""

import json
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

MAX_EVIDENCE_ATTRIBUTES_BYTES = 16 * 1024
FindingText = Annotated[str, Field(min_length=1, max_length=500)]


class EvidenceKind(StrEnum):
    """第一版只读调查工具能够产生的证据类型。"""

    LOG = "log"
    METRIC = "metric"
    CHANGE = "change"


class EvidenceRelation(StrEnum):
    """证据对主根因假设的确定性关系。"""

    SUPPORTS = "supports"
    REFUTES = "refutes"
    CONTEXT = "context"


class HypothesisStatus(StrEnum):
    """质疑节点对主根因假设的审查结果。"""

    SUPPORTED = "supported"
    CONTESTED = "contested"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence"


class RootCause(StrEnum):
    """MVP 故障集中的根因分类。"""

    RELEASE_500_REGRESSION = "release_500_regression"
    DATABASE_POOL_MISCONFIGURATION = "database_pool_misconfiguration"
    REDIS_UNAVAILABLE = "redis_unavailable"
    UNDETERMINED = "undetermined"


class Recommendation(StrEnum):
    """调查阶段只能建议动作，不能直接执行。"""

    ROLLBACK_RELEASE = "rollback_release"
    ROLLBACK_CONFIGURATION = "rollback_configuration"
    RESTORE_DEPENDENCY = "restore_dependency"
    CONTINUE_INVESTIGATION = "continue_investigation"

    @property
    def requires_human_approval(self) -> bool:
        """所有会改变被管系统状态的建议都必须经过人工审批。"""

        return self is not Recommendation.CONTINUE_INVESTIGATION


class InvestigationMode(StrEnum):
    """用于单 Agent 与多 Agent 公平对照的运行模式。"""

    SINGLE_AGENT = "single_agent"
    MULTI_AGENT = "multi_agent"


class InvestigationDataSourceMode(StrEnum):
    """一次持久调查使用冻结证据还是真实现场证据。"""

    FROZEN = "frozen"
    LIVE = "live"


class InvestigationRunStatus(StrEnum):
    """持久化调查运行状态。"""

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class WorkItemStatus(StrEnum):
    """持久任务的租约处理状态。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"


class WorkItemKind(StrEnum):
    """Worker 可以确定性路由的任务类型。"""

    RUN_SINGLE_AGENT_INVESTIGATION = "run_single_agent_investigation"
    RUN_MULTI_AGENT_INVESTIGATION = "run_multi_agent_investigation"


class StrictModel(BaseModel):
    """调查模块值对象的共同约束。"""

    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceObservation(StrictModel):
    """只读工具返回的一条可引用观察结果。"""

    evidence_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
    kind: EvidenceKind
    source: str = Field(min_length=1, max_length=100)
    summary: str = Field(min_length=1, max_length=500)
    observed_at: datetime
    attributes: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        """所有证据时间都必须明确时区并归一化为 UTC。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("observed_at 必须包含时区")
        return value.astimezone(UTC)

    @field_validator("attributes")
    @classmethod
    def limit_attributes_size(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        """阻止工具把无限制原始日志伪装成证据属性写入业务库。"""

        serialized = json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(serialized) > MAX_EVIDENCE_ATTRIBUTES_BYTES:
            raise ValueError("attributes 序列化后不能超过 16 KiB")
        return value


class InvestigationRequest(StrictModel):
    """启动一次单 Agent 调查所需的最小事故上下文。"""

    incident_id: UUID
    service_name: str = Field(min_length=1, max_length=100)
    title: str = Field(min_length=1, max_length=200)
    started_at: datetime

    @field_validator("started_at")
    @classmethod
    def normalize_started_at(cls, value: datetime) -> datetime:
        """拒绝无法确定时间线顺序的无时区输入。"""

        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("started_at 必须包含时区")
        return value.astimezone(UTC)


class InvestigationConclusion(StrictModel):
    """模型必须输出并通过校验的调查结论。"""

    root_cause: RootCause
    confidence: float = Field(ge=0, le=1)
    summary: str = Field(min_length=1, max_length=1000)
    evidence_ids: tuple[str, ...] = Field(min_length=1, max_length=20)
    recommendation: Recommendation
    requires_human_approval: bool
    safety_notes: tuple[str, ...] = Field(default_factory=tuple, max_length=10)

    @field_validator("evidence_ids")
    @classmethod
    def ensure_unique_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """重复引用不会增加证据强度，因此直接拒绝。"""

        if len(set(value)) != len(value):
            raise ValueError("evidence_ids 不能重复")
        return value

    @model_validator(mode="after")
    def enforce_approval_boundary(self) -> Self:
        """回滚只能作为待审批建议，不能伪装成已授权操作。"""

        if self.recommendation.requires_human_approval and not self.requires_human_approval:
            raise ValueError("处置建议必须标记为需要人工审批")
        return self


class InvestigationRunResult(StrictModel):
    """一次 Agent 运行的业务结果与最小可评测统计。"""

    conclusion: InvestigationConclusion
    returned_evidence_ids: tuple[str, ...]
    tool_call_count: int = Field(ge=0)

    @field_validator("returned_evidence_ids")
    @classmethod
    def ensure_unique_returned_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        """工具重复返回同一证据时只保留一个业务引用。"""

        if len(set(value)) != len(value):
            raise ValueError("returned_evidence_ids 不能重复")
        return value


class InvestigationArtifacts(StrictModel):
    """一次调查实际产生、可进入 Evidence Board 的业务材料。"""

    result: InvestigationRunResult
    evidence_items: tuple[EvidenceObservation, ...] = Field(min_length=1, max_length=100)
    specialist_reports: tuple["SpecialistReport", ...] = Field(
        default_factory=tuple,
        max_length=10,
    )

    @model_validator(mode="after")
    def validate_evidence_references(self) -> Self:
        """持久化前再次阻断遗漏、重复和跨专业证据引用。"""

        evidence_by_id = {item.evidence_id: item for item in self.evidence_items}
        if len(evidence_by_id) != len(self.evidence_items):
            raise ValueError("Evidence Board 的 evidence_id 不能重复")

        returned_ids = set(self.result.returned_evidence_ids)
        if returned_ids != set(evidence_by_id):
            raise ValueError("returned_evidence_ids 必须与 Evidence Board 证据完全一致")
        if set(self.result.conclusion.evidence_ids) - returned_ids:
            raise ValueError("调查结论引用了 Evidence Board 中不存在的证据")

        specialists = [report.specialist for report in self.specialist_reports]
        if len(set(specialists)) != len(specialists):
            raise ValueError("同一次调查不能保存重复专业报告")
        for report in self.specialist_reports:
            for evidence_id in report.evidence_ids:
                evidence = evidence_by_id.get(evidence_id)
                if evidence is None:
                    raise ValueError("专业报告引用了 Evidence Board 中不存在的证据")
                if evidence.kind is not report.specialist:
                    raise ValueError("专业报告只能引用同类专业证据")
        return self


class InvestigationMetrics(StrictModel):
    """当前基线可离线计算并持久化的核心指标。"""

    top1_accuracy: float = Field(ge=0, le=1)
    evidence_recall: float = Field(ge=0, le=1)
    recommendation_accuracy: float = Field(ge=0, le=1)
    safety_violation_rate: float = Field(ge=0, le=1)
    tool_call_count: int = Field(ge=0)


class SpecialistReport(StrictModel):
    """一个专业调查节点对其证据范围的结构化分析。"""

    specialist: EvidenceKind
    summary: str = Field(min_length=1, max_length=1000)
    findings: tuple[FindingText, ...] = Field(min_length=1, max_length=20)
    evidence_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    confidence: float = Field(ge=0, le=1)

    @field_validator("evidence_ids")
    @classmethod
    def ensure_unique_report_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("专业调查报告的 evidence_ids 不能重复")
        return value


class HypothesisEvidenceAssessment(StrictModel):
    """一条证据与主根因假设之间的可审计判断。"""

    evidence_id: str = Field(min_length=1, max_length=100)
    relation: EvidenceRelation
    rule_code: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9_]*$",
    )
    rationale: str = Field(min_length=1, max_length=500)


class HypothesisChallenge(StrictModel):
    """确定性质疑节点生成的主根因假设与证据关系。"""

    root_cause: RootCause
    statement: str = Field(min_length=1, max_length=1000)
    confidence: float = Field(ge=0, le=1)
    status: HypothesisStatus
    summary: str = Field(min_length=1, max_length=1000)
    rule_version: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-z0-9][a-z0-9._-]*$",
    )
    evidence_assessments: tuple[HypothesisEvidenceAssessment, ...] = Field(
        min_length=1,
        max_length=100,
    )

    @field_validator("evidence_assessments")
    @classmethod
    def ensure_unique_assessed_evidence(
        cls,
        value: tuple[HypothesisEvidenceAssessment, ...],
    ) -> tuple[HypothesisEvidenceAssessment, ...]:
        evidence_ids = [item.evidence_id for item in value]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("质疑结果不能重复评估同一证据")
        return value


class MultiAgentInvestigationResult(InvestigationArtifacts):
    """LangGraph 并行调查结果及各专业节点报告。"""

    specialist_reports: tuple[SpecialistReport, ...] = Field(min_length=1, max_length=10)
    hypothesis_challenge: HypothesisChallenge


class InvestigationComparison(StrictModel):
    """同一评测场景下单 Agent 与多 Agent 的指标差异。"""

    single_agent: InvestigationMetrics
    multi_agent: InvestigationMetrics
    top1_accuracy_delta: float = Field(ge=-1, le=1)
    evidence_recall_delta: float = Field(ge=-1, le=1)
    safety_violation_rate_delta: float = Field(ge=-1, le=1)
    tool_call_count_delta: int


class WorkItemError(StrictModel):
    """经过长度限制、可安全持久化的 Worker 错误摘要。"""

    error_code: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1, max_length=500)
