"""调查运行和持久任务的框架无关读取模型。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from pydantic import JsonValue

from opsswarm.incidents.persistence import StoredIncident
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceRelation,
    HypothesisStatus,
    InvestigationConclusion,
    InvestigationDataSourceMode,
    InvestigationMetrics,
    InvestigationMode,
    InvestigationRequest,
    InvestigationRunResult,
    InvestigationRunStatus,
    RootCause,
    SpecialistReport,
    StrictModel,
    WorkItemError,
    WorkItemKind,
    WorkItemStatus,
)
from opsswarm.observability import TraceContextCarrier


class StoredInvestigationRun(StrictModel):
    """业务库中的一次调查运行快照。"""

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


class StoredWorkItem(StrictModel):
    """持久任务及当前租约快照。"""

    id: UUID
    investigation_run_id: UUID
    kind: WorkItemKind
    status: WorkItemStatus
    idempotency_key: str
    available_at: datetime
    locked_by: str | None
    locked_until: datetime | None
    lease_token: UUID | None
    attempt_count: int
    max_attempts: int
    last_error: WorkItemError | None
    trace_context: TraceContextCarrier | None = None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class StoredEvidenceItem(StrictModel):
    """Evidence Board 中一条规范化证据的业务快照。"""

    id: UUID
    investigation_run_id: UUID
    evidence_id: str
    kind: EvidenceKind
    source: str
    summary: str
    observed_at: datetime
    attributes: dict[str, JsonValue]
    content_hash: str
    created_at: datetime


class StoredSpecialistReport(StrictModel):
    """专业报告及其同一次调查内的证据引用。"""

    id: UUID
    investigation_run_id: UUID
    specialist: EvidenceKind
    summary: str
    findings: tuple[str, ...]
    evidence_ids: tuple[str, ...]
    confidence: float
    created_at: datetime

    def to_domain(self) -> SpecialistReport:
        """转换为不带数据库标识的专业报告值对象。"""

        return SpecialistReport(
            specialist=self.specialist,
            summary=self.summary,
            findings=self.findings,
            evidence_ids=self.evidence_ids,
            confidence=self.confidence,
        )


class StoredHypothesisEvidence(StrictModel):
    """持久化假设对一条证据的质疑判断。"""

    evidence_item_id: UUID
    evidence_id: str
    relation: EvidenceRelation
    rule_code: str
    rationale: str
    created_at: datetime


class StoredHypothesis(StrictModel):
    """主根因假设、状态及完整证据关系。"""

    id: UUID
    investigation_run_id: UUID
    root_cause: RootCause
    statement: str
    confidence: float
    status: HypothesisStatus
    challenge_summary: str
    rule_version: str
    evidence_assessments: tuple[StoredHypothesisEvidence, ...]
    created_at: datetime


class StoredEvidenceBoard(StrictModel):
    """工程师审查一次调查所需的完整证据视图。"""

    investigation_run_id: UUID
    status: InvestigationRunStatus
    conclusion: InvestigationConclusion | None
    evidence_items: tuple[StoredEvidenceItem, ...]
    specialist_reports: tuple[StoredSpecialistReport, ...]
    hypotheses: tuple[StoredHypothesis, ...]


@dataclass(frozen=True, slots=True)
class StartedInvestigation:
    """开始调查事务一次返回的三个一致快照。"""

    incident: StoredIncident
    run: StoredInvestigationRun
    work_item: StoredWorkItem


@dataclass(frozen=True, slots=True)
class ClaimedInvestigation:
    """Worker 领取后执行处理器所需的最小上下文。"""

    run: StoredInvestigationRun
    work_item: StoredWorkItem
    request: InvestigationRequest
