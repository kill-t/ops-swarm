"""调查运行和持久任务的框架无关读取模型。"""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from opsswarm.incidents.persistence import StoredIncident
from opsswarm.investigation.models import (
    InvestigationMetrics,
    InvestigationMode,
    InvestigationRequest,
    InvestigationRunResult,
    InvestigationRunStatus,
    StrictModel,
    WorkItemError,
    WorkItemKind,
    WorkItemStatus,
)


class StoredInvestigationRun(StrictModel):
    """业务库中的一次调查运行快照。"""

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
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


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
