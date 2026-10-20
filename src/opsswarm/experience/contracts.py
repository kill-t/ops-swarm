"""经验候选来源闭包与有界事实草稿契约。"""

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from opsswarm.approval.models import ApprovalRequestStatus
from opsswarm.execution import ExecutionStatus
from opsswarm.execution.persistence.records import StoredOperationExecution
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    EvidenceRelation,
    InvestigationDataSourceMode,
    InvestigationRunStatus,
)
from opsswarm.investigation.persistence.records import StoredHypothesis
from opsswarm.planning import ActionPlanStatus, action_plan_content_hash
from opsswarm.planning.persistence.records import StoredActionPlan
from opsswarm.reports.models import StoredIncidentReport
from opsswarm.verification import VerificationStatus
from opsswarm.verification.persistence.records import StoredVerificationRun
from opsswarm.workroom.models import WorkroomInvestigationSummary

GENERATION_POLICY_VERSION: Literal["report-fact-draft-v1"] = "report-fact-draft-v1"
MAX_SOURCE_BYTES = 128 * 1024
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
ShortText = Annotated[str, Field(min_length=1, max_length=500)]


class ExperienceError(Exception):
    """只公开稳定错误，不把原始来源或校验异常写入任务错误。"""

    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)


def canonical_bytes(value: BaseModel) -> bytes:
    """内容身份只依赖规范化字段，不包含任务创建时间。"""
    return json.dumps(
        value.model_dump(mode="json"),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def canonical_hash(value: BaseModel) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def _utc_values(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("经验来源时间必须包含时区")
        return value.astimezone(UTC)
    if isinstance(value, BaseModel):
        return _utc_values(value.model_dump(mode="python"))
    if isinstance(value, dict):
        return {key: _utc_values(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_utc_values(item) for item in value]
    return value


def _require_utc_times(value: Any) -> None:
    """旧嵌套契约解析字符串后再次检查，避免无时区时间绕过外层预校验。"""
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() != timedelta(0):
            raise ValueError("经验来源时间必须使用带时区的 UTC")
    elif isinstance(value, BaseModel):
        _require_utc_times(value.model_dump(mode="python"))
    elif isinstance(value, dict):
        for item in value.values():
            _require_utc_times(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _require_utc_times(item)


class ExperienceModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    @model_validator(mode="before")
    @classmethod
    def normalize_times(cls, value: Any) -> Any:
        return _utc_values(value)


class SourceEvidence(ExperienceModel):
    id: UUID
    investigation_run_id: UUID
    observation: EvidenceObservation
    content_hash: Hash

    @model_validator(mode="after")
    def validate_hash(self) -> Self:
        if canonical_hash(self.observation) != self.content_hash:
            raise ValueError("经验来源证据内容与散列不一致")
        return self


class ExperienceSource(ExperienceModel):
    """报告精确绑定的完整处置事实；不读取后来产生的最新记录。"""

    source_schema_version: Literal["experience-source-v1"] = "experience-source-v1"
    report_id: UUID
    report_content_hash: Hash
    incident_id: UUID
    service_name: str = Field(min_length=1, max_length=100)
    data_source_mode: InvestigationDataSourceMode
    environment: Literal["local_fault_lab"] = "local_fault_lab"
    report: StoredIncidentReport
    investigation: WorkroomInvestigationSummary
    hypothesis: StoredHypothesis
    evidence: tuple[SourceEvidence, ...] = Field(min_length=1, max_length=20)
    plan: StoredActionPlan
    approval_plan_content_hash: Hash
    execution: StoredOperationExecution
    verification: StoredVerificationRun

    @model_validator(mode="after")
    def validate_closure(self) -> Self:
        _require_utc_times(self)
        report = self.report
        room = report.content.workroom
        run = self.investigation
        hypothesis = self.hypothesis
        plan = self.plan
        execution = self.execution
        verification = self.verification
        if (
            self.report_id != report.id
            or self.report_content_hash != report.content_hash
            or canonical_hash(report.content) != report.content_hash
            or self.incident_id != report.incident_id
            or self.service_name != report.content.incident.service_name
            or room.investigation != run
            or self.data_source_mode != run.data_source_mode
        ):
            raise ValueError("经验来源报告身份或调查摘要不一致")
        if run.status is not InvestigationRunStatus.SUCCEEDED:
            raise ValueError("经验来源调查尚未成功完成")
        if room.evidence_board is None or room.evidence_board.primary_hypothesis is None:
            raise ValueError("经验来源缺少主假设")
        summary = room.evidence_board.primary_hypothesis
        if (
            hypothesis.investigation_run_id != run.id
            or any(
                getattr(hypothesis, name) != getattr(summary, name)
                for name in (
                    "id",
                    "root_cause",
                    "statement",
                    "confidence",
                    "status",
                    "rule_version",
                )
            )
            or hypothesis.challenge_summary != summary.challenge_summary
        ):
            raise ValueError("经验来源主假设与报告不一致")
        ids = {item.id: item for item in self.evidence}
        keys = {item.observation.evidence_id for item in self.evidence}
        if (
            len(ids) != len(self.evidence)
            or len(keys) != len(self.evidence)
            or room.evidence_board.evidence_count != len(self.evidence)
            or any(item.investigation_run_id != run.id for item in self.evidence)
            or not hypothesis.evidence_assessments
            or len({item.evidence_item_id for item in hypothesis.evidence_assessments})
            != len(hypothesis.evidence_assessments)
            or any(
                item.evidence_item_id not in ids
                or ids[item.evidence_item_id].observation.evidence_id != item.evidence_id
                for item in hypothesis.evidence_assessments
            )
        ):
            raise ValueError("经验来源证据引用没有闭包")
        if (
            room.action_plan is None
            or plan.status is not ActionPlanStatus.APPROVED
            or plan.id != room.action_plan.id
            or plan.investigation_run_id != run.id
            or plan.hypothesis_id != hypothesis.id
            or plan.version != room.action_plan.version
            or plan.status != room.action_plan.status
            or plan.summary != room.action_plan.summary
            or plan.risk_level != room.action_plan.risk_level
            or plan.requires_human_approval != room.action_plan.requires_human_approval
            or len(plan.steps) != room.action_plan.step_count
            or not 1 <= len(plan.steps) <= 20
            or room.operation_execution is None
            or execution.id != room.operation_execution.id
            or execution.incident_id != self.incident_id
            or execution.action_plan_id != plan.id
            or execution.action_plan_version != plan.version
            or execution.action_step_id != room.operation_execution.action_step_id
            or execution.status is not ExecutionStatus.SUCCEEDED
            or execution.status != room.operation_execution.status
            or room.approval_request is None
            or room.approval_request.status is not ApprovalRequestStatus.APPROVED
            or execution.approval_request_id != room.approval_request.id
        ):
            raise ValueError("经验来源计划与执行没有精确绑定")
        if (
            action_plan_content_hash(
                plan.to_domain(),
                plan_id=plan.id,
                investigation_run_id=plan.investigation_run_id,
                hypothesis_id=plan.hypothesis_id,
            )
            != self.approval_plan_content_hash
        ):
            raise ValueError("经验来源计划与历史审批的内容散列不一致")
        step = next((item for item in plan.steps if item.id == execution.action_step_id), None)
        summary_verification = room.verification_run
        if (
            summary_verification is None
            or verification.id != summary_verification.id
            or verification.incident_id != self.incident_id
            or verification.operation_execution_id != execution.id
            or verification.action_step_id != execution.action_step_id
            or step is None
            or step.verification != verification.rule
            or verification.status is not VerificationStatus.PASSED
            or verification.status != summary_verification.status
            or verification.rule.signal != summary_verification.signal
            or verification.rule.threshold != summary_verification.threshold
            or verification.rule.observation_window_seconds
            != summary_verification.observation_window_seconds
            or verification.rule.required_successful_samples
            != summary_verification.required_successful_samples
            or verification.successful_sample_count != summary_verification.successful_sample_count
            or len(verification.samples) != summary_verification.sample_count
            or not 1 <= len(verification.samples) <= 100
            or len({item.id for item in verification.samples}) != len(verification.samples)
            or verification.last_observed_at != summary_verification.last_observed_at
            or verification.completed_at != summary_verification.completed_at
        ):
            raise ValueError("经验来源验证没有精确绑定")
        if len(canonical_bytes(self)) > MAX_SOURCE_BYTES:
            raise ExperienceError("source_too_large", "经验来源超过大小上限")
        return self


class ExperienceEvidenceReference(ExperienceModel):
    evidence_item_id: UUID
    evidence_id: str = Field(min_length=1, max_length=100)
    content_hash: Hash
    kind: EvidenceKind
    relations: tuple[EvidenceRelation, ...] = Field(max_length=1)


class ExperienceDraft(ExperienceModel):
    """待人工审查的历史事实投影，不携带执行参数或授权。"""

    schema_version: Literal["experience-candidate-v1"] = "experience-candidate-v1"
    generation_method: Literal["deterministic_report_projection"] = (
        "deterministic_report_projection"
    )
    generation_policy_version: Literal["report-fact-draft-v1"] = GENERATION_POLICY_VERSION
    service_name: str = Field(min_length=1, max_length=100)
    data_source_mode: InvestigationDataSourceMode
    environment: Literal["local_fault_lab"] = "local_fault_lab"
    historical_summary: str = Field(min_length=1, max_length=2000)
    fault_tags: tuple[ShortText, ...] = Field(min_length=1, max_length=5)
    preconditions: tuple[ShortText, ...] = Field(min_length=1, max_length=10)
    evidence_refs: tuple[ExperienceEvidenceReference, ...] = Field(min_length=1, max_length=20)
    sample_ids: tuple[UUID, ...] = Field(min_length=1, max_length=100)
    hypothesis_status: str = Field(min_length=1, max_length=100)
    verifier_name: str = Field(min_length=1, max_length=100)
    recheck_requirement: Literal["使用前必须重新调查当前事故并核对适用范围及反证"] = (
        "使用前必须重新调查当前事故并核对适用范围及反证"
    )
    authorization_limit: Literal["历史审批及操作不授予当前事故的审批或执行权限"] = (
        "历史审批及操作不授予当前事故的审批或执行权限"
    )
