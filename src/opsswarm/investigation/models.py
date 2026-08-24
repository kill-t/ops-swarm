"""调查输入、证据与结构化结论。"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator


class EvidenceKind(StrEnum):
    """第一版只读调查工具能够产生的证据类型。"""

    LOG = "log"
    METRIC = "metric"
    CHANGE = "change"


class RootCause(StrEnum):
    """MVP 故障集中的根因分类。"""

    RELEASE_500_REGRESSION = "release_500_regression"
    DATABASE_POOL_MISCONFIGURATION = "database_pool_misconfiguration"
    REDIS_UNAVAILABLE = "redis_unavailable"
    UNDETERMINED = "undetermined"


class Recommendation(StrEnum):
    """调查阶段只能建议动作，不能直接执行。"""

    ROLLBACK_RELEASE = "rollback_release"
    CONTINUE_INVESTIGATION = "continue_investigation"


class InvestigationMode(StrEnum):
    """用于单 Agent 与未来多 Agent 公平对照的运行模式。"""

    SINGLE_AGENT = "single_agent"


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

        if (
            self.recommendation is Recommendation.ROLLBACK_RELEASE
            and not self.requires_human_approval
        ):
            raise ValueError("回滚版本建议必须标记为需要人工审批")
        return self


class InvestigationRunResult(StrictModel):
    """一次 Agent 运行的业务结果与最小可评测统计。"""

    conclusion: InvestigationConclusion
    returned_evidence_ids: tuple[str, ...]
    tool_call_count: int = Field(ge=0)


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
    findings: tuple[str, ...] = Field(min_length=1, max_length=20)
    evidence_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=20)
    confidence: float = Field(ge=0, le=1)

    @field_validator("evidence_ids")
    @classmethod
    def ensure_unique_report_evidence_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("专业调查报告的 evidence_ids 不能重复")
        return value


class MultiAgentInvestigationResult(StrictModel):
    """LangGraph 并行调查结果及各专业节点报告。"""

    result: InvestigationRunResult
    specialist_reports: tuple[SpecialistReport, ...]


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
