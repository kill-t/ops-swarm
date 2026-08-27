"""作战室跨领域只读投影的稳定类型。"""

from datetime import UTC, datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from opsswarm.approval import ApprovalRequestStatus
from opsswarm.execution import ExecutionStatus
from opsswarm.investigation.models import (
    HypothesisStatus,
    InvestigationMode,
    InvestigationRunStatus,
    RootCause,
)
from opsswarm.planning import ActionPlanStatus, RiskLevel
from opsswarm.verification import VerificationStatus


class StrictWorkroomModel(BaseModel):
    """作战室投影只允许已声明且不可变的字段。"""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class WorkroomInvestigationSummary(StrictWorkroomModel):
    """当前 Incident 最近一次调查运行的摘要。"""

    id: UUID
    mode: InvestigationMode
    status: InvestigationRunStatus
    prompt_version: str
    model_name: str | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime

    @field_validator("created_at", "started_at", "completed_at", "updated_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("调查时间必须包含时区")
        return value.astimezone(UTC)


class WorkroomHypothesisSummary(StrictWorkroomModel):
    """Evidence Board 中经过确定性质疑的主根因摘要。"""

    id: UUID
    root_cause: RootCause
    statement: str
    confidence: float = Field(ge=0, le=1, allow_inf_nan=False)
    status: HypothesisStatus
    challenge_summary: str
    rule_version: str


class WorkroomEvidenceBoardSummary(StrictWorkroomModel):
    """不复制原始证据内容的 Evidence Board 汇总。"""

    evidence_count: int = Field(ge=0)
    specialist_report_count: int = Field(ge=0)
    primary_hypothesis: WorkroomHypothesisSummary | None


class WorkroomActionPlanSummary(StrictWorkroomModel):
    """最近调查的当前处置计划版本摘要。"""

    id: UUID
    version: int = Field(ge=1)
    status: ActionPlanStatus
    summary: str
    risk_level: RiskLevel
    requires_human_approval: bool
    step_count: int = Field(ge=1)
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def normalize_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("计划时间必须包含时区")
        return value.astimezone(UTC)


class WorkroomApprovalSummary(StrictWorkroomModel):
    """当前计划唯一审批请求的审计摘要。"""

    id: UUID
    status: ApprovalRequestStatus
    requested_by_id: str
    request_reason: str
    expires_at: datetime
    created_at: datetime
    decided_by_id: str | None
    decision_reason: str | None
    decided_at: datetime | None

    @field_validator("expires_at", "created_at", "decided_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("审批时间必须包含时区")
        return value.astimezone(UTC)


class WorkroomExecutionSummary(StrictWorkroomModel):
    """当前计划最近一次白名单操作执行摘要。"""

    id: UUID
    action_step_id: UUID
    status: ExecutionStatus
    executor_name: str
    attempt_count: int = Field(ge=1)
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @field_validator("created_at", "updated_at", "completed_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("执行时间必须包含时区")
        return value.astimezone(UTC)


class WorkroomVerificationSummary(StrictWorkroomModel):
    """最近操作所关联的恢复验证进度摘要。"""

    id: UUID
    status: VerificationStatus
    signal: str
    threshold: float = Field(ge=0, le=1, allow_inf_nan=False)
    observation_window_seconds: int = Field(ge=1)
    required_successful_samples: int = Field(ge=1)
    successful_sample_count: int = Field(ge=0)
    sample_count: int = Field(ge=0)
    failure_code: str | None
    failure_message: str | None
    last_observed_at: datetime | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @field_validator("last_observed_at", "created_at", "updated_at", "completed_at")
    @classmethod
    def normalize_time(cls, value: datetime | None) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("验证时间必须包含时区")
        return value.astimezone(UTC)


class WorkroomSnapshot(StrictWorkroomModel):
    """一次数据库快照中的完整事故处置链路。"""

    incident_id: UUID
    incident_version: int = Field(ge=1)
    investigation: WorkroomInvestigationSummary | None
    evidence_board: WorkroomEvidenceBoardSummary | None
    action_plan: WorkroomActionPlanSummary | None
    approval_request: WorkroomApprovalSummary | None
    operation_execution: WorkroomExecutionSummary | None
    verification_run: WorkroomVerificationSummary | None

    @model_validator(mode="after")
    def validate_stage_order(self) -> Self:
        stages = (
            self.investigation,
            self.evidence_board,
            self.action_plan,
            self.approval_request,
            self.operation_execution,
            self.verification_run,
        )
        seen_gap = False
        for stage in stages:
            if stage is None:
                seen_gap = True
            elif seen_gap:
                raise ValueError("作战室处置阶段不能跳过前置记录")
        return self
