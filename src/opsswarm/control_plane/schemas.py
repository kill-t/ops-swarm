"""控制面 API 的请求与响应模型。"""

from datetime import datetime
from typing import Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator

from opsswarm.incidents import (
    ActorType,
    AlertIngestionResult,
    AlertSubmission,
    IncidentEventType,
    IncidentSeverity,
    IncidentStatus,
)
from opsswarm.incidents.persistence import StoredIncident, StoredIncidentEvent
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceRelation,
    HypothesisStatus,
    InvestigationConclusion,
    InvestigationMetrics,
    InvestigationMode,
    InvestigationRunResult,
    InvestigationRunStatus,
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
    StoredInvestigationRun,
    StoredSpecialistReport,
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


class InvestigationRunResponse(StrictApiModel):
    """持久化调查运行的 HTTP 表示。"""

    id: UUID
    incident_id: UUID
    mode: InvestigationMode
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
