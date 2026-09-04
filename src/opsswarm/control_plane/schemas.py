"""控制面 API 的请求与响应模型。"""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from opsswarm.approval.models import ApprovalRequestStatus
from opsswarm.approval.persistence import (
    ApprovalDecisionOutcome,
    ApprovalRequestCreation,
    PendingApprovalPage,
    StoredApprovalRequest,
    StoredPendingApproval,
)
from opsswarm.evaluation.attempts import (
    EvaluationAttemptBudget,
    EvaluationAttemptPage,
    EvaluationAttemptStatus,
    EvaluationFailureCategory,
    StoredEvaluationAttempt,
)
from opsswarm.evaluation.metadata import (
    EvaluationExperimentSpec,
    EvaluationModeTelemetry,
    EvaluationRunTelemetry,
)
from opsswarm.evaluation.models import (
    InvestigationAggregateDelta,
    InvestigationAggregateMetrics,
    InvestigationBenchmarkReport,
    InvestigationModeBenchmark,
)
from opsswarm.evaluation.persistence import (
    EvaluationRunPage,
    StoredEvaluationRun,
    StoredEvaluationRunSummary,
)
from opsswarm.execution import ExecutionStatus
from opsswarm.execution.persistence import (
    ExecutionCandidatePage,
    OperationExecutionOutcome,
    StoredExecutionCandidate,
    StoredOperationExecution,
)
from opsswarm.follow_up import (
    ReportActionItem,
    ReportActionItemCompletion,
    ReportActionItemCreation,
    ReportActionItemEvent,
    ReportActionItemEventType,
    ReportActionItemGovernanceEntry,
    ReportActionItemGovernancePage,
    ReportActionItemPage,
    ReportActionItemStatus,
)
from opsswarm.incidents import (
    ActorType,
    AlertIngestionResult,
    AlertSubmission,
    IncidentEventType,
    IncidentSeverity,
    IncidentStatus,
)
from opsswarm.incidents.persistence import StoredIncident, StoredIncidentEvent
from opsswarm.investigation.attempts import (
    InvestigationAttemptStatus,
    ModelPricing,
    ModelUsageTelemetry,
)
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceRelation,
    HypothesisStatus,
    InvestigationConclusion,
    InvestigationDataSourceMode,
    InvestigationMetrics,
    InvestigationMode,
    InvestigationRunResult,
    InvestigationRunStatus,
    Recommendation,
    RootCause,
    WorkItemError,
    WorkItemStatus,
)
from opsswarm.investigation.persistence import (
    StartedInvestigation,
    StoredEvidenceBoard,
    StoredEvidenceItem,
    StoredHypothesis,
    StoredHypothesisEvidence,
    StoredInvestigationAttempt,
    StoredInvestigationRun,
    StoredSpecialistReport,
)
from opsswarm.planning.models import (
    ActionParameters,
    ActionPlanCapability,
    ActionPlanCapabilityReason,
    ActionPlanCapabilityStatus,
    ActionPlanStatus,
    ActionType,
    RiskLevel,
    TargetType,
    VerificationRule,
)
from opsswarm.planning.persistence import (
    ActionPlanCreation,
    StoredActionPlan,
    StoredActionStep,
)
from opsswarm.reports import IncidentReportContent, IncidentReportCreation, StoredIncidentReport
from opsswarm.verification import (
    VerificationSampleStatus,
    VerificationStatus,
    VerificationWorkItemStatus,
)
from opsswarm.verification.persistence import (
    StoredVerificationCandidate,
    StoredVerificationRun,
    StoredVerificationSample,
    StoredVerificationWorkItem,
    VerificationCandidatePage,
    VerificationRunCreation,
    VerificationSampleOutcome,
)
from opsswarm.workroom import (
    WorkroomActionPlanSummary,
    WorkroomApprovalSummary,
    WorkroomEvidenceBoardSummary,
    WorkroomExecutionSummary,
    WorkroomInvestigationSummary,
    WorkroomSnapshot,
    WorkroomVerificationSummary,
)


class StrictApiModel(BaseModel):
    """拒绝未声明字段，避免调用方误以为参数已经生效。"""

    model_config = ConfigDict(extra="forbid")


class AlertRequest(StrictApiModel):
    """外部告警接收请求；接收时间由服务器生成。"""

    source: str = Field(min_length=1, max_length=100)
    external_event_id: str = Field(min_length=1, max_length=200)
    fingerprint: str = Field(min_length=1, max_length=200)
    service_name: str = Field(min_length=1, max_length=100)
    severity: IncidentSeverity
    title: str = Field(min_length=1, max_length=200)
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at 必须包含时区")
        return value

    def to_submission(self, *, ingested_at: datetime) -> AlertSubmission:
        """转换为不依赖 FastAPI 的告警领域输入。"""

        return AlertSubmission(
            source=self.source,
            external_event_id=self.external_event_id,
            fingerprint=self.fingerprint,
            service_name=self.service_name,
            severity=self.severity,
            title=self.title,
            payload=self.payload,
            occurred_at=self.occurred_at,
            ingested_at=ingested_at,
        )


class StartInvestigationRequest(StrictApiModel):
    """开始调查动作的乐观锁与审计信息。"""

    expected_version: int = Field(ge=1)
    actor_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)
    mode: InvestigationMode = InvestigationMode.SINGLE_AGENT
    data_source_mode: InvestigationDataSourceMode = InvestigationDataSourceMode.FROZEN


class CloseIncidentRequest(StrictApiModel):
    """工程师确认关闭事故所需的乐观锁和审计信息。"""

    expected_version: int = Field(ge=1)
    actor_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)


class InvestigationRunResponse(StrictApiModel):
    """持久化调查运行的 HTTP 表示。"""

    id: UUID
    incident_id: UUID
    mode: InvestigationMode
    data_source_mode: InvestigationDataSourceMode
    status: InvestigationRunStatus
    prompt_version: str
    graph_thread_id: str | None
    model_name: str | None
    result: InvestigationRunResult | None
    metrics: InvestigationMetrics | None
    last_error: WorkItemError | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    updated_at: datetime

    @classmethod
    def from_stored(cls, stored: StoredInvestigationRun) -> Self:
        return cls.model_validate(stored.model_dump())


class InvestigationAttemptResponse(StrictApiModel):
    """不暴露租约令牌的 Worker 尝试审计视图。"""

    id: UUID
    work_item_id: UUID
    attempt_number: int
    status: InvestigationAttemptStatus
    worker_id: str
    model_name: str | None
    pricing: ModelPricing | None
    usage: ModelUsageTelemetry
    started_at: datetime
    lease_expires_at: datetime
    finished_at: datetime | None
    duration_ms: int | None
    will_retry: bool | None
    error: WorkItemError | None

    @classmethod
    def from_stored(cls, stored: StoredInvestigationAttempt) -> Self:
        return cls.model_validate(stored.model_dump(exclude={"investigation_run_id"}))


class InvestigationAttemptListResponse(StrictApiModel):
    """一次调查中按序排列的全部尝试。"""

    investigation_run_id: UUID
    items: tuple[InvestigationAttemptResponse, ...]

    @classmethod
    def from_stored(
        cls,
        run_id: UUID,
        attempts: tuple[StoredInvestigationAttempt, ...],
    ) -> Self:
        return cls(
            investigation_run_id=run_id,
            items=tuple(InvestigationAttemptResponse.from_stored(item) for item in attempts),
        )


class EvidenceItemResponse(StrictApiModel):
    """Evidence Board 中一条可校验内容哈希的规范化证据。"""

    id: UUID
    evidence_id: str
    kind: EvidenceKind
    source: str
    summary: str
    observed_at: datetime
    attributes: dict[str, JsonValue]
    content_hash: str
    created_at: datetime

    @classmethod
    def from_stored(cls, stored: StoredEvidenceItem) -> Self:
        return cls.model_validate(stored.model_dump(exclude={"investigation_run_id"}))


class SpecialistReportResponse(StrictApiModel):
    """专业节点报告及其显式证据引用。"""

    id: UUID
    specialist: EvidenceKind
    summary: str
    findings: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    confidence: float
    created_at: datetime

    @classmethod
    def from_stored(cls, stored: StoredSpecialistReport) -> Self:
        return cls.model_validate(stored.model_dump(exclude={"investigation_run_id"}))


class HypothesisEvidenceResponse(StrictApiModel):
    """确定性规则对一条证据的支持、反驳或上下文判断。"""

    evidence_item_id: UUID
    evidence_id: str
    relation: EvidenceRelation
    rule_code: str
    rationale: str
    created_at: datetime

    @classmethod
    def from_stored(cls, stored: StoredHypothesisEvidence) -> Self:
        return cls.model_validate(stored.model_dump())


class HypothesisResponse(StrictApiModel):
    """主根因假设及完整质疑结果。"""

    id: UUID
    root_cause: RootCause
    statement: str
    confidence: float
    status: HypothesisStatus
    challenge_summary: str
    rule_version: str
    evidence_assessments: tuple[HypothesisEvidenceResponse, ...]
    created_at: datetime

    @classmethod
    def from_stored(cls, stored: StoredHypothesis) -> Self:
        return cls(
            id=stored.id,
            root_cause=stored.root_cause,
            statement=stored.statement,
            confidence=stored.confidence,
            status=stored.status,
            challenge_summary=stored.challenge_summary,
            rule_version=stored.rule_version,
            evidence_assessments=tuple(
                HypothesisEvidenceResponse.from_stored(item) for item in stored.evidence_assessments
            ),
            created_at=stored.created_at,
        )


class EvidenceBoardResponse(StrictApiModel):
    """一次调查可供工程师审查的结论、证据和专业报告。"""

    investigation_run_id: UUID
    status: InvestigationRunStatus
    conclusion: InvestigationConclusion | None
    evidence_items: tuple[EvidenceItemResponse, ...]
    specialist_reports: tuple[SpecialistReportResponse, ...]
    hypotheses: tuple[HypothesisResponse, ...]

    @classmethod
    def from_stored(cls, stored: StoredEvidenceBoard) -> Self:
        return cls(
            investigation_run_id=stored.investigation_run_id,
            status=stored.status,
            conclusion=stored.conclusion,
            evidence_items=tuple(
                EvidenceItemResponse.from_stored(item) for item in stored.evidence_items
            ),
            specialist_reports=tuple(
                SpecialistReportResponse.from_stored(report) for report in stored.specialist_reports
            ),
            hypotheses=tuple(
                HypothesisResponse.from_stored(hypothesis) for hypothesis in stored.hypotheses
            ),
        )


class ActionPlanCapabilityResponse(StrictApiModel):
    """调查建议进入确定性处置计划模板的当前能力。"""

    investigation_run_id: UUID
    root_cause: RootCause
    recommendation: Recommendation
    hypothesis_status: HypothesisStatus
    status: ActionPlanCapabilityStatus
    action_type: ActionType | None
    template_version: str | None
    requires_human_approval: bool
    can_create: bool
    reason_code: ActionPlanCapabilityReason
    explanation: str

    @classmethod
    def from_capability(
        cls,
        run_id: UUID,
        capability: ActionPlanCapability,
    ) -> Self:
        return cls(
            investigation_run_id=run_id,
            **capability.model_dump(),
        )


class ActionStepResponse(StrictApiModel):
    """计划中尚未授权执行的白名单动作。"""

    id: UUID
    step_order: int
    action_type: ActionType
    target_type: TargetType
    target_name: str
    parameters: ActionParameters
    risk_level: RiskLevel
    requires_human_approval: bool
    risk_rule_code: str
    risk_rationale: str
    verification: VerificationRule
    created_at: datetime

    @classmethod
    def from_stored(cls, stored: StoredActionStep) -> Self:
        return cls.model_validate(stored.model_dump())


class ActionPlanResponse(StrictApiModel):
    """绑定根因假设版本的结构化处置计划草案。"""

    id: UUID
    investigation_run_id: UUID
    hypothesis_id: UUID
    version: int
    status: ActionPlanStatus
    summary: str
    risk_level: RiskLevel
    requires_human_approval: bool
    risk_policy_version: str
    steps: tuple[ActionStepResponse, ...]
    created_at: datetime

    @classmethod
    def from_stored(cls, stored: StoredActionPlan) -> Self:
        return cls(
            id=stored.id,
            investigation_run_id=stored.investigation_run_id,
            hypothesis_id=stored.hypothesis_id,
            version=stored.version,
            status=stored.status,
            summary=stored.summary,
            risk_level=stored.risk_level,
            requires_human_approval=stored.requires_human_approval,
            risk_policy_version=stored.risk_policy_version,
            steps=tuple(ActionStepResponse.from_stored(step) for step in stored.steps),
            created_at=stored.created_at,
        )


class CreateActionPlanResponse(StrictApiModel):
    """计划首次创建或幂等读取结果。"""

    created: bool
    action_plan: ActionPlanResponse

    @classmethod
    def from_creation(cls, creation: ActionPlanCreation) -> Self:
        return cls(
            created=creation.created,
            action_plan=ActionPlanResponse.from_stored(creation.plan),
        )


class CreateApprovalRequest(StrictApiModel):
    """提交计划审批时的乐观锁与人工审计信息。"""

    expected_incident_version: int = Field(ge=1)
    actor_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)


class ApprovalRequestResponse(StrictApiModel):
    """绑定精确计划版本和内容摘要的审批请求。"""

    id: UUID
    incident_id: UUID
    action_plan_id: UUID
    action_plan_version: int
    plan_content_hash: str
    status: ApprovalRequestStatus
    requested_by_type: ActorType
    requested_by_id: str
    request_reason: str
    expires_at: datetime
    created_at: datetime
    decided_by_type: ActorType | None
    decided_by_id: str | None
    decision_reason: str | None
    decided_at: datetime | None

    @classmethod
    def from_stored(cls, stored: StoredApprovalRequest) -> Self:
        return cls.model_validate(stored.model_dump())


class PendingApprovalIncidentResponse(StrictApiModel):
    """审批人识别事故和提交乐观锁所需的最小快照。"""

    id: UUID
    display_number: int
    reference: str
    service_name: str
    title: str
    severity: IncidentSeverity
    status: IncidentStatus
    version: int
    updated_at: datetime


class PendingApprovalResponse(StrictApiModel):
    """一条待办包含精确审批绑定、Incident 版本和完整计划。"""

    approval_request: ApprovalRequestResponse
    incident: PendingApprovalIncidentResponse
    action_plan: ActionPlanResponse

    @classmethod
    def from_stored(cls, stored: StoredPendingApproval) -> Self:
        incident = stored.incident
        return cls(
            approval_request=ApprovalRequestResponse.from_stored(stored.approval_request),
            incident=PendingApprovalIncidentResponse(
                **incident.model_dump(),
                reference=f"INC-{incident.display_number:06d}",
            ),
            action_plan=ActionPlanResponse.from_stored(stored.action_plan),
        )


class PendingApprovalPageResponse(StrictApiModel):
    """有界待审批队列，total 用于提示是否还有未展示待办。"""

    items: tuple[PendingApprovalResponse, ...]
    total: int

    @classmethod
    def from_page(cls, page: PendingApprovalPage) -> Self:
        return cls(
            items=tuple(PendingApprovalResponse.from_stored(item) for item in page.items),
            total=page.total,
        )


class CreateApprovalRequestResponse(StrictApiModel):
    """审批请求首次创建或幂等读取结果。"""

    created: bool
    approval_request: ApprovalRequestResponse

    @classmethod
    def from_creation(cls, creation: ApprovalRequestCreation) -> Self:
        return cls(
            created=creation.created,
            approval_request=ApprovalRequestResponse.from_stored(creation.request),
        )


class DecideApprovalRequest(StrictApiModel):
    """人工决定使用的 Incident 乐观锁和审计信息。"""

    expected_incident_version: int = Field(ge=1)
    actor_id: str = Field(min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=500)


class DecideApprovalRequestResponse(StrictApiModel):
    """首次应用决定或同方向幂等重试的结果。"""

    decision_applied: bool
    approval_request: ApprovalRequestResponse

    @classmethod
    def from_outcome(cls, outcome: ApprovalDecisionOutcome) -> Self:
        return cls(
            decision_applied=outcome.decision_applied,
            approval_request=ApprovalRequestResponse.from_stored(outcome.request),
        )


class ExecuteOperationRequest(StrictApiModel):
    """领取白名单步骤时的幂等键和 Incident 乐观锁。"""

    idempotency_key: str = Field(
        min_length=8,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    expected_incident_version: int = Field(ge=1)


class OperationExecutionResponse(StrictApiModel):
    """不暴露内部租约令牌的执行记录。"""

    id: UUID
    idempotency_key: str
    incident_id: UUID
    approval_request_id: UUID
    action_plan_id: UUID
    action_plan_version: int
    action_step_id: UUID
    status: ExecutionStatus
    executor_name: str
    incident_version_before: int
    attempt_count: int
    locked_until: datetime | None
    result: dict[str, JsonValue] | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_stored(cls, stored: StoredOperationExecution) -> Self:
        return cls(
            id=stored.id,
            idempotency_key=stored.idempotency_key,
            incident_id=stored.incident_id,
            approval_request_id=stored.approval_request_id,
            action_plan_id=stored.action_plan_id,
            action_plan_version=stored.action_plan_version,
            action_step_id=stored.action_step_id,
            status=stored.status,
            executor_name=stored.executor_name,
            incident_version_before=stored.incident_version_before,
            attempt_count=stored.attempt_count,
            locked_until=stored.locked_until,
            result=stored.result.output if stored.result is not None else None,
            error_code=stored.failure.error_code if stored.failure is not None else None,
            error_message=stored.failure.message if stored.failure is not None else None,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
            completed_at=stored.completed_at,
        )


class ExecutionCandidateIncidentResponse(StrictApiModel):
    """待执行计划关联的 Incident 当前快照。"""

    id: UUID
    display_number: int
    reference: str
    service_name: str
    title: str
    severity: IncidentSeverity
    status: IncidentStatus
    version: int
    updated_at: datetime


class ExecutionCandidateResponse(StrictApiModel):
    """精确批准绑定、完整计划和正在运行的幂等记录。"""

    approval_request: ApprovalRequestResponse
    incident: ExecutionCandidateIncidentResponse
    action_plan: ActionPlanResponse
    active_executions: tuple[OperationExecutionResponse, ...]

    @classmethod
    def from_stored(cls, stored: StoredExecutionCandidate) -> Self:
        incident = stored.incident
        return cls(
            approval_request=ApprovalRequestResponse.from_stored(stored.approval_request),
            incident=ExecutionCandidateIncidentResponse(
                **incident.model_dump(),
                reference=f"INC-{incident.display_number:06d}",
            ),
            action_plan=ActionPlanResponse.from_stored(stored.action_plan),
            active_executions=tuple(
                OperationExecutionResponse.from_stored(item) for item in stored.active_executions
            ),
        )


class ExecutionCandidatePageResponse(StrictApiModel):
    """有界待执行队列，total 用于提示是否还有未展示计划。"""

    items: tuple[ExecutionCandidateResponse, ...]
    total: int

    @classmethod
    def from_page(cls, page: ExecutionCandidatePage) -> Self:
        return cls(
            items=tuple(ExecutionCandidateResponse.from_stored(item) for item in page.items),
            total=page.total,
        )


class ExecuteOperationResponse(StrictApiModel):
    """区分新记录、租约恢复和纯幂等读取。"""

    created: bool
    execution_performed: bool
    operation_execution: OperationExecutionResponse

    @classmethod
    def from_outcome(cls, outcome: OperationExecutionOutcome) -> Self:
        return cls(
            created=outcome.created,
            execution_performed=outcome.execution_performed,
            operation_execution=OperationExecutionResponse.from_stored(outcome.execution),
        )


class CreateVerificationRunRequest(StrictApiModel):
    """开始恢复验证时使用的 Incident 乐观锁版本。"""

    expected_incident_version: int = Field(ge=1)


class VerificationSampleRequest(StrictApiModel):
    """一次服务端探针采样的幂等标识。"""

    idempotency_key: str = Field(
        min_length=8,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )


class VerificationSampleResponse(StrictApiModel):
    """一个不可由客户端提交观测值的持久样本。"""

    id: UUID
    verification_run_id: UUID
    idempotency_key: str
    sequence: int
    status: VerificationSampleStatus
    signal: str
    observed_value: float | None
    threshold: float
    http_status: int | None
    error_code: str | None
    error_message: str | None
    observed_at: datetime

    @classmethod
    def from_stored(cls, stored: StoredVerificationSample) -> Self:
        return cls(
            id=stored.id,
            verification_run_id=stored.verification_run_id,
            idempotency_key=stored.idempotency_key,
            sequence=stored.sequence,
            status=stored.status,
            signal=stored.signal,
            observed_value=stored.observed_value,
            threshold=stored.threshold,
            http_status=stored.http_status,
            error_code=stored.failure.error_code if stored.failure is not None else None,
            error_message=stored.failure.message if stored.failure is not None else None,
            observed_at=stored.observed_at,
        )


class VerificationRunResponse(StrictApiModel):
    """冻结规则、进度、终态和全部样本的恢复验证。"""

    id: UUID
    operation_execution_id: UUID
    incident_id: UUID
    action_step_id: UUID
    status: VerificationStatus
    verifier_name: str
    incident_version_before: int
    rule: VerificationRule
    successful_sample_count: int
    first_success_at: datetime | None
    last_observed_at: datetime | None
    error_code: str | None
    error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None
    samples: tuple[VerificationSampleResponse, ...]

    @classmethod
    def from_stored(cls, stored: StoredVerificationRun) -> Self:
        return cls(
            id=stored.id,
            operation_execution_id=stored.operation_execution_id,
            incident_id=stored.incident_id,
            action_step_id=stored.action_step_id,
            status=stored.status,
            verifier_name=stored.verifier_name,
            incident_version_before=stored.incident_version_before,
            rule=stored.rule,
            successful_sample_count=stored.successful_sample_count,
            first_success_at=stored.first_success_at,
            last_observed_at=stored.last_observed_at,
            error_code=stored.failure.error_code if stored.failure is not None else None,
            error_message=stored.failure.message if stored.failure is not None else None,
            created_at=stored.created_at,
            updated_at=stored.updated_at,
            completed_at=stored.completed_at,
            samples=tuple(VerificationSampleResponse.from_stored(item) for item in stored.samples),
        )


class CreateVerificationRunResponse(StrictApiModel):
    """区分首次创建和按成功操作幂等读取。"""

    created: bool
    verification_run: VerificationRunResponse

    @classmethod
    def from_creation(cls, creation: VerificationRunCreation) -> Self:
        return cls(
            created=creation.created,
            verification_run=VerificationRunResponse.from_stored(creation.run),
        )


class VerificationWorkItemResponse(StrictApiModel):
    """不暴露租约令牌的恢复验证调度状态。"""

    id: UUID
    verification_run_id: UUID
    status: VerificationWorkItemStatus
    next_sample_sequence: int
    available_at: datetime
    locked_by: str | None
    locked_until: datetime | None
    attempt_count: int
    consecutive_failure_count: int
    last_error_code: str | None
    last_error_message: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None

    @classmethod
    def from_stored(cls, stored: StoredVerificationWorkItem) -> Self:
        return cls(
            id=stored.id,
            verification_run_id=stored.verification_run_id,
            status=stored.status,
            next_sample_sequence=stored.next_sample_sequence,
            available_at=stored.available_at,
            locked_by=stored.locked_by,
            locked_until=stored.locked_until,
            attempt_count=stored.attempt_count,
            consecutive_failure_count=stored.consecutive_failure_count,
            last_error_code=(
                stored.last_error.error_code if stored.last_error is not None else None
            ),
            last_error_message=(
                stored.last_error.message if stored.last_error is not None else None
            ),
            created_at=stored.created_at,
            updated_at=stored.updated_at,
            completed_at=stored.completed_at,
        )


class VerificationCandidateIncidentResponse(StrictApiModel):
    """待验证操作关联的 Incident 当前快照。"""

    id: UUID
    display_number: int
    reference: str
    service_name: str
    title: str
    severity: IncidentSeverity
    status: IncidentStatus
    version: int
    updated_at: datetime


class VerificationCandidateResponse(StrictApiModel):
    """当前计划成功操作及其可能已经开始的验证运行。"""

    incident: VerificationCandidateIncidentResponse
    operation_execution: OperationExecutionResponse
    action_step: ActionStepResponse
    verification_run: VerificationRunResponse | None
    work_item: VerificationWorkItemResponse | None

    @classmethod
    def from_stored(cls, stored: StoredVerificationCandidate) -> Self:
        incident = stored.incident
        return cls(
            incident=VerificationCandidateIncidentResponse(
                **incident.model_dump(),
                reference=f"INC-{incident.display_number:06d}",
            ),
            operation_execution=OperationExecutionResponse.from_stored(stored.operation_execution),
            action_step=ActionStepResponse.from_stored(stored.action_step),
            verification_run=(
                VerificationRunResponse.from_stored(stored.verification_run)
                if stored.verification_run is not None
                else None
            ),
            work_item=(
                VerificationWorkItemResponse.from_stored(stored.work_item)
                if stored.work_item is not None
                else None
            ),
        )


class VerificationCandidatePageResponse(StrictApiModel):
    """有界待验证队列，total 用于提示是否还有未展示操作。"""

    items: tuple[VerificationCandidateResponse, ...]
    total: int

    @classmethod
    def from_page(cls, page: VerificationCandidatePage) -> Self:
        return cls(
            items=tuple(VerificationCandidateResponse.from_stored(item) for item in page.items),
            total=page.total,
        )


class RecordVerificationSampleResponse(StrictApiModel):
    """一次采样的运行汇总与幂等结果。"""

    created: bool
    probe_performed: bool
    verification_run: VerificationRunResponse
    sample: VerificationSampleResponse

    @classmethod
    def from_outcome(cls, outcome: VerificationSampleOutcome) -> Self:
        return cls(
            created=outcome.created,
            probe_performed=outcome.probe_performed,
            verification_run=VerificationRunResponse.from_stored(outcome.run),
            sample=VerificationSampleResponse.from_stored(outcome.sample),
        )


class IncidentResponse(StrictApiModel):
    """Incident 当前快照的 HTTP 表示。"""

    id: UUID
    display_number: int
    reference: str
    service_name: str
    title: str
    severity: IncidentSeverity
    status: IncidentStatus
    fingerprint: str
    version: int
    started_at: datetime
    resolved_at: datetime | None
    closed_at: datetime | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_stored(cls, stored: StoredIncident) -> Self:
        incident = stored.incident
        return cls(
            id=incident.id,
            display_number=stored.display_number,
            reference=f"INC-{stored.display_number:06d}",
            service_name=incident.service_name,
            title=incident.title,
            severity=incident.severity,
            status=incident.status,
            fingerprint=incident.fingerprint,
            version=incident.version,
            started_at=incident.started_at,
            resolved_at=incident.resolved_at,
            closed_at=incident.closed_at,
            created_at=incident.created_at,
            updated_at=incident.updated_at,
        )


class IncidentWorkroomResponse(StrictApiModel):
    """同一数据库快照中的事故调查与处置链路。"""

    incident_id: UUID
    incident_version: int
    investigation: WorkroomInvestigationSummary | None
    evidence_board: WorkroomEvidenceBoardSummary | None
    action_plan: WorkroomActionPlanSummary | None
    approval_request: WorkroomApprovalSummary | None
    operation_execution: WorkroomExecutionSummary | None
    verification_run: WorkroomVerificationSummary | None

    @classmethod
    def from_snapshot(cls, snapshot: WorkroomSnapshot) -> Self:
        return cls.model_validate(snapshot.model_dump())


class CreateIncidentReportRequest(StrictApiModel):
    """生成复盘快照时使用的乐观锁版本和人工身份。"""

    expected_incident_version: int = Field(ge=1)
    actor_id: str = Field(min_length=1, max_length=100)


class IncidentReportResponse(StrictApiModel):
    """不可变事故复盘快照及其内容摘要。"""

    id: UUID
    incident_id: UUID
    incident_version: int
    schema_version: str
    content_hash: str
    content: IncidentReportContent
    created_at: datetime

    @classmethod
    def from_stored(cls, stored: StoredIncidentReport) -> Self:
        return cls.model_validate(stored.model_dump())


class CreateIncidentReportResponse(StrictApiModel):
    """区分首次冻结和幂等读取的报告响应。"""

    created: bool
    report: IncidentReportResponse

    @classmethod
    def from_creation(cls, creation: IncidentReportCreation) -> Self:
        return cls(
            created=creation.created,
            report=IncidentReportResponse.from_stored(creation.report),
        )


class CreateReportActionItemRequest(StrictApiModel):
    """由工程师为冻结报告登记一项可追踪的改进工作。"""

    idempotency_key: str = Field(
        min_length=8,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]+$",
    )
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(min_length=1, max_length=1000)
    owner_id: str = Field(min_length=1, max_length=100)
    due_at: datetime
    actor_id: str = Field(min_length=1, max_length=100)


class CompleteReportActionItemRequest(StrictApiModel):
    """完成行动项时提交乐观锁版本和不可变审计说明。"""

    expected_version: int = Field(ge=1)
    actor_id: str = Field(min_length=1, max_length=100)
    completion_note: str = Field(min_length=1, max_length=500)


class ReportActionItemResponse(StrictApiModel):
    """报告改进行动项的当前状态。"""

    id: UUID
    report_id: UUID
    idempotency_key: str
    title: str
    description: str
    owner_id: str
    status: ReportActionItemStatus
    version: int
    due_at: datetime
    created_by_id: str
    created_at: datetime
    completed_by_id: str | None
    completion_note: str | None
    completed_at: datetime | None

    @classmethod
    def from_item(cls, item: ReportActionItem) -> Self:
        return cls.model_validate(item.model_dump())


class CreateReportActionItemResponse(StrictApiModel):
    """区分首次创建和相同命令幂等重放。"""

    created: bool
    action_item: ReportActionItemResponse

    @classmethod
    def from_creation(cls, creation: ReportActionItemCreation) -> Self:
        return cls(
            created=creation.created,
            action_item=ReportActionItemResponse.from_item(creation.item),
        )


class CompleteReportActionItemResponse(StrictApiModel):
    """区分首次完成和相同完成命令重试。"""

    completion_applied: bool
    action_item: ReportActionItemResponse

    @classmethod
    def from_completion(cls, completion: ReportActionItemCompletion) -> Self:
        return cls(
            completion_applied=completion.completion_applied,
            action_item=ReportActionItemResponse.from_item(completion.item),
        )


class ReportActionItemPageResponse(StrictApiModel):
    """一个报告下待完成优先的有界行动项列表。"""

    items: tuple[ReportActionItemResponse, ...]
    total: int

    @classmethod
    def from_page(cls, page: ReportActionItemPage) -> Self:
        return cls(
            items=tuple(ReportActionItemResponse.from_item(item) for item in page.items),
            total=page.total,
        )


class ReportActionItemGovernanceIncidentResponse(StrictApiModel):
    """治理列表定位行动项来源所需的最小 Incident 上下文。"""

    id: UUID
    display_number: int
    reference: str
    service_name: str
    title: str
    severity: IncidentSeverity


class ReportActionItemGovernanceEntryResponse(StrictApiModel):
    """跨事故待办中的行动项和来源事故。"""

    action_item: ReportActionItemResponse
    incident: ReportActionItemGovernanceIncidentResponse
    overdue: bool

    @classmethod
    def from_entry(cls, entry: ReportActionItemGovernanceEntry) -> Self:
        return cls(
            action_item=ReportActionItemResponse.from_item(entry.action_item),
            incident=ReportActionItemGovernanceIncidentResponse(
                id=entry.incident_id,
                display_number=entry.incident_display_number,
                reference=f"INC-{entry.incident_display_number:06d}",
                service_name=entry.incident_service_name,
                title=entry.incident_title,
                severity=entry.incident_severity,
            ),
            overdue=entry.overdue,
        )


class ReportActionItemGovernancePageResponse(StrictApiModel):
    """服务器时间判定的跨事故未完成行动项治理页。"""

    items: tuple[ReportActionItemGovernanceEntryResponse, ...]
    total: int
    as_of: datetime
    owner_id: str | None
    overdue_only: bool

    @classmethod
    def from_page(cls, page: ReportActionItemGovernancePage) -> Self:
        return cls(
            items=tuple(
                ReportActionItemGovernanceEntryResponse.from_entry(item) for item in page.items
            ),
            total=page.total,
            as_of=page.as_of,
            owner_id=page.owner_id,
            overdue_only=page.overdue_only,
        )


class ReportActionItemEventResponse(StrictApiModel):
    """一次不可覆盖的行动项状态事件。"""

    cursor: int
    item_id: UUID
    event_type: ReportActionItemEventType
    actor_id: str
    from_status: ReportActionItemStatus | None
    to_status: ReportActionItemStatus
    reason: str
    item_version: int
    occurred_at: datetime

    @classmethod
    def from_event(cls, event: ReportActionItemEvent) -> Self:
        return cls.model_validate(event.model_dump())


class ReportActionItemEventListResponse(StrictApiModel):
    """一个行动项的完整追加式审计时间线。"""

    item_id: UUID
    items: tuple[ReportActionItemEventResponse, ...]


class StartInvestigationResponse(StrictApiModel):
    """原子开始调查后返回事故、运行和持久任务标识。"""

    incident: IncidentResponse
    investigation: InvestigationRunResponse
    work_item_id: UUID
    work_item_status: WorkItemStatus

    @classmethod
    def from_started(cls, started: StartedInvestigation) -> Self:
        return cls(
            incident=IncidentResponse.from_stored(started.incident),
            investigation=InvestigationRunResponse.from_stored(started.run),
            work_item_id=started.work_item.id,
            work_item_status=started.work_item.status,
        )


class AlertIngestionResponse(StrictApiModel):
    """告警接收结果以及幂等判定。"""

    incident: IncidentResponse
    alert_id: UUID
    deduplicated: bool
    created_incident: bool

    @classmethod
    def from_result(cls, result: AlertIngestionResult) -> Self:
        return cls(
            incident=IncidentResponse.from_stored(
                StoredIncident(
                    incident=result.incident,
                    display_number=result.display_number,
                )
            ),
            alert_id=result.alert_id,
            deduplicated=result.deduplicated,
            created_incident=result.created_incident,
        )


class IncidentPageResponse(StrictApiModel):
    """基于展示序号倒序游标的 Incident 页面。"""

    items: list[IncidentResponse]
    next_before_display_number: int | None


class EvaluationRunSummaryResponse(StrictApiModel):
    """评测面板列表所需的版本、运行器和聚合指标。"""

    id: UUID
    sequence: int
    dataset_key: str
    dataset_content_hash: str
    scoring_policy_version: str
    scenario_count: int
    runner_name: str
    experiment: EvaluationExperimentSpec
    repetition_index: int
    telemetry: EvaluationRunTelemetry
    generated_at: datetime
    created_at: datetime
    single_agent: InvestigationAggregateMetrics
    multi_agent: InvestigationAggregateMetrics
    delta: InvestigationAggregateDelta

    @classmethod
    def from_stored(cls, stored: StoredEvaluationRunSummary) -> Self:
        return cls.model_validate(stored.model_dump())


class EvaluationRunPageResponse(StrictApiModel):
    """按运行序号倒序游标返回的评测历史。"""

    items: list[EvaluationRunSummaryResponse]
    next_before_sequence: int | None

    @classmethod
    def from_page(cls, page: EvaluationRunPage) -> Self:
        return cls(
            items=[EvaluationRunSummaryResponse.from_stored(item) for item in page.items],
            next_before_sequence=page.next_before_sequence,
        )


class EvaluationRunResponse(EvaluationRunSummaryResponse):
    """一次评测运行的完整、可复核报告。"""

    idempotency_key: str
    report_content_hash: str
    report: InvestigationBenchmarkReport

    @classmethod
    def from_run(cls, stored: StoredEvaluationRun) -> Self:
        return cls.model_validate(stored.model_dump())


class EvaluationAttemptResponse(StrictApiModel):
    """一条真实模型评测尝试的预算、遥测和终态。"""

    id: UUID
    sequence: int
    dataset_key: str
    dataset_content_hash: str
    experiment: EvaluationExperimentSpec
    runner_name: str
    repetition_index: int
    scenario_id: str
    mode: InvestigationMode
    idempotency_key: str
    status: EvaluationAttemptStatus
    budget: EvaluationAttemptBudget
    telemetry: EvaluationModeTelemetry
    result: InvestigationModeBenchmark | None
    failure_category: EvaluationFailureCategory | None
    failure_message: str | None
    started_at: datetime
    finished_at: datetime | None
    created_at: datetime

    @classmethod
    def from_stored(cls, stored: StoredEvaluationAttempt) -> Self:
        return cls.model_validate(stored.model_dump())


class EvaluationAttemptPageResponse(StrictApiModel):
    """按尝试序号倒序游标返回的真实模型调用历史。"""

    items: list[EvaluationAttemptResponse]
    next_before_sequence: int | None

    @classmethod
    def from_page(cls, page: EvaluationAttemptPage) -> Self:
        return cls(
            items=[EvaluationAttemptResponse.from_stored(item) for item in page.items],
            next_before_sequence=page.next_before_sequence,
        )


class IncidentEventResponse(StrictApiModel):
    """可通过全局游标重放的一条 Incident 事件。"""

    cursor: int
    incident_id: UUID
    event_type: IncidentEventType
    actor_type: ActorType
    actor_id: str | None
    from_status: IncidentStatus | None
    to_status: IncidentStatus
    reason: str
    incident_version: int
    payload: dict[str, JsonValue]
    occurred_at: datetime

    @classmethod
    def from_stored(cls, stored: StoredIncidentEvent) -> Self:
        event = stored.event
        return cls(
            cursor=stored.cursor,
            incident_id=event.incident_id,
            event_type=event.event_type,
            actor_type=event.actor.actor_type,
            actor_id=event.actor.actor_id,
            from_status=event.from_status,
            to_status=event.to_status,
            reason=event.reason,
            incident_version=event.incident_version,
            payload=dict(stored.payload),
            occurred_at=event.occurred_at,
        )


class IncidentEventPageResponse(StrictApiModel):
    """基于全局事件号正序游标的时间线页面。"""

    items: list[IncidentEventResponse]
    next_after_cursor: int | None


class ApiErrorResponse(StrictApiModel):
    """领域错误的稳定 HTTP 表示。"""

    error_code: str
    message: str
    details: dict[str, JsonValue] = Field(default_factory=dict)


class HealthResponse(StrictApiModel):
    """控制面进程存活响应。"""

    service: str = "opsswarm-control-plane"
    status: str = "healthy"
