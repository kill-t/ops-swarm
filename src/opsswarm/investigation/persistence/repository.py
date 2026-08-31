"""调查运行、Evidence Board 与 PostgreSQL 租约任务仓储。"""

import hashlib
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from uuid import UUID, uuid4, uuid5

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents import IncidentAction, IncidentActor
from opsswarm.incidents.persistence.repository import transition_incident_in_session
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.attempts import (
    InvestigationAttemptStatus,
    ModelPricing,
    ModelUsageTelemetry,
)
from opsswarm.investigation.challenge import validate_hypothesis_challenge
from opsswarm.investigation.errors import (
    GraphThreadIdMismatch,
    InvestigationRunNotFound,
    WorkItemLeaseLost,
    WorkItemNotFound,
)
from opsswarm.investigation.graph_thread import build_graph_thread_id
from opsswarm.investigation.models import (
    EvidenceKind,
    EvidenceObservation,
    EvidenceRelation,
    HypothesisChallenge,
    HypothesisStatus,
    InvestigationArtifacts,
    InvestigationDataSourceMode,
    InvestigationMetrics,
    InvestigationMode,
    InvestigationRequest,
    InvestigationRunResult,
    InvestigationRunStatus,
    RootCause,
    SpecialistReport,
    WorkItemError,
    WorkItemKind,
    WorkItemStatus,
)
from opsswarm.investigation.persistence.records import (
    ClaimedInvestigation,
    StartedInvestigation,
    StoredEvidenceBoard,
    StoredEvidenceItem,
    StoredHypothesis,
    StoredHypothesisEvidence,
    StoredInvestigationAttempt,
    StoredInvestigationRun,
    StoredSpecialistReport,
    StoredWorkItem,
)
from opsswarm.investigation.persistence.tables import (
    EvidenceItemRow,
    HypothesisEvidenceRow,
    HypothesisRow,
    InvestigationAttemptRow,
    InvestigationRunRow,
    SpecialistReportEvidenceRow,
    SpecialistReportRow,
    WorkItemRow,
)
from opsswarm.investigation.prompt_versions import (
    MULTI_AGENT_PROMPT_VERSION as CURRENT_MULTI_AGENT_PROMPT_VERSION,
)
from opsswarm.investigation.prompt_versions import (
    SINGLE_AGENT_PROMPT_VERSION as CURRENT_SINGLE_AGENT_PROMPT_VERSION,
)
from opsswarm.observability import TraceContextCarrier

DEFAULT_PROMPT_VERSION = CURRENT_SINGLE_AGENT_PROMPT_VERSION
MULTI_AGENT_PROMPT_VERSION = CURRENT_MULTI_AGENT_PROMPT_VERSION

_PROMPT_VERSION_BY_MODE = {
    InvestigationMode.SINGLE_AGENT: DEFAULT_PROMPT_VERSION,
    InvestigationMode.MULTI_AGENT: MULTI_AGENT_PROMPT_VERSION,
}
_WORK_KIND_BY_MODE = {
    InvestigationMode.SINGLE_AGENT: WorkItemKind.RUN_SINGLE_AGENT_INVESTIGATION,
    InvestigationMode.MULTI_AGENT: WorkItemKind.RUN_MULTI_AGENT_INVESTIGATION,
}


class InvestigationStore:
    """原子创建调查任务，并提供带租约令牌的 Worker 状态迁移。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def start_investigation(
        self,
        incident_id: UUID,
        *,
        expected_version: int,
        occurred_at: datetime,
        actor: IncidentActor,
        reason: str,
        max_attempts: int = 3,
        mode: InvestigationMode = InvestigationMode.SINGLE_AGENT,
        data_source_mode: InvestigationDataSourceMode = InvestigationDataSourceMode.FROZEN,
        trace_context: TraceContextCarrier | None = None,
    ) -> StartedInvestigation:
        """在同一事务中迁移 Incident、创建运行并投递持久任务。"""

        occurred_at = _normalize_time(occurred_at, field_name="occurred_at")
        if not 1 <= max_attempts <= 10:
            raise ValueError("max_attempts 必须在 1 到 10 之间")

        async with self._session_factory() as session, session.begin():
            incident = await transition_incident_in_session(
                session,
                incident_id,
                action=IncidentAction.START_INVESTIGATION,
                expected_version=expected_version,
                occurred_at=occurred_at,
                actor=actor,
                reason=reason,
            )
            run_id = uuid4()
            run_row = InvestigationRunRow(
                id=run_id,
                incident_id=incident_id,
                mode=mode.value,
                data_source_mode=data_source_mode.value,
                status=InvestigationRunStatus.QUEUED.value,
                prompt_version=_PROMPT_VERSION_BY_MODE[mode],
                graph_thread_id=(
                    build_graph_thread_id(run_id) if mode is InvestigationMode.MULTI_AGENT else None
                ),
                model_name=None,
                result=None,
                metrics=None,
                last_error=None,
                created_at=occurred_at,
                started_at=None,
                completed_at=None,
                updated_at=occurred_at,
            )
            work_row = WorkItemRow(
                id=uuid4(),
                investigation_run_id=run_row.id,
                kind=_WORK_KIND_BY_MODE[mode].value,
                status=WorkItemStatus.PENDING.value,
                idempotency_key=f"start-investigation:{incident_id}:{incident.incident.version}",
                available_at=occurred_at,
                locked_by=None,
                locked_until=None,
                lease_token=None,
                attempt_count=0,
                max_attempts=max_attempts,
                last_error=None,
                trace_context=(
                    trace_context.model_dump(mode="json", exclude_none=True)
                    if trace_context is not None
                    else None
                ),
                created_at=occurred_at,
                updated_at=occurred_at,
                completed_at=None,
            )
            session.add_all([run_row, work_row])
            await session.flush()
            return StartedInvestigation(
                incident=incident,
                run=_stored_run(run_row),
                work_item=_stored_work_item(work_row),
            )

    async def get_run(self, run_id: UUID) -> StoredInvestigationRun:
        """读取一个调查运行。"""

        async with self._session_factory() as session:
            row = await session.get(InvestigationRunRow, run_id)
            if row is None:
                raise InvestigationRunNotFound(run_id)
            return _stored_run(row)

    async def get_evidence_board(self, run_id: UUID) -> StoredEvidenceBoard:
        """读取按稳定顺序组装的证据、专业报告及其引用。"""

        async with self._session_factory() as session:
            run_row = await session.get(InvestigationRunRow, run_id)
            if run_row is None:
                raise InvestigationRunNotFound(run_id)

            evidence_rows = list(
                await session.scalars(
                    select(EvidenceItemRow)
                    .where(EvidenceItemRow.investigation_run_id == run_id)
                    .order_by(EvidenceItemRow.evidence_key)
                )
            )
            report_rows = list(
                await session.scalars(
                    select(SpecialistReportRow)
                    .where(SpecialistReportRow.investigation_run_id == run_id)
                    .order_by(SpecialistReportRow.specialist)
                )
            )
            link_rows = list(
                await session.scalars(
                    select(SpecialistReportEvidenceRow).where(
                        SpecialistReportEvidenceRow.investigation_run_id == run_id
                    )
                )
            )
            hypothesis_rows = list(
                await session.scalars(
                    select(HypothesisRow)
                    .where(HypothesisRow.investigation_run_id == run_id)
                    .order_by(HypothesisRow.created_at, HypothesisRow.id)
                )
            )
            hypothesis_evidence_rows = list(
                await session.scalars(
                    select(HypothesisEvidenceRow).where(
                        HypothesisEvidenceRow.investigation_run_id == run_id
                    )
                )
            )
            evidence_key_by_id = {row.id: row.evidence_key for row in evidence_rows}
            evidence_ids_by_report: defaultdict[UUID, list[str]] = defaultdict(list)
            for link in link_rows:
                evidence_ids_by_report[link.specialist_report_id].append(
                    evidence_key_by_id[link.evidence_item_id]
                )
            assessments_by_hypothesis: defaultdict[UUID, list[StoredHypothesisEvidence]] = (
                defaultdict(list)
            )
            for relation in hypothesis_evidence_rows:
                assessments_by_hypothesis[relation.hypothesis_id].append(
                    _stored_hypothesis_evidence(
                        relation,
                        evidence_id=evidence_key_by_id[relation.evidence_item_id],
                    )
                )

            return StoredEvidenceBoard(
                investigation_run_id=run_id,
                status=InvestigationRunStatus(run_row.status),
                conclusion=(
                    InvestigationRunResult.model_validate(run_row.result).conclusion
                    if run_row.result is not None
                    else None
                ),
                evidence_items=tuple(_stored_evidence_item(row) for row in evidence_rows),
                specialist_reports=tuple(
                    _stored_specialist_report(
                        row,
                        evidence_ids=tuple(sorted(evidence_ids_by_report[row.id])),
                    )
                    for row in report_rows
                ),
                hypotheses=tuple(
                    _stored_hypothesis(
                        row,
                        evidence_assessments=tuple(
                            sorted(
                                assessments_by_hypothesis[row.id],
                                key=lambda item: item.evidence_id,
                            )
                        ),
                    )
                    for row in hypothesis_rows
                ),
            )

    async def get_work_item(self, work_item_id: UUID) -> StoredWorkItem:
        """读取一个持久任务。"""

        async with self._session_factory() as session:
            row = await session.get(WorkItemRow, work_item_id)
            if row is None:
                raise WorkItemNotFound(work_item_id)
            return _stored_work_item(row)

    async def list_attempts(self, run_id: UUID) -> tuple[StoredInvestigationAttempt, ...]:
        """按尝试序号读取一次调查的完整 Worker 历史。"""

        async with self._session_factory() as session:
            if await session.get(InvestigationRunRow, run_id) is None:
                raise InvestigationRunNotFound(run_id)
            rows = await session.scalars(
                select(InvestigationAttemptRow)
                .where(InvestigationAttemptRow.investigation_run_id == run_id)
                .order_by(InvestigationAttemptRow.attempt_number)
            )
            return tuple(_stored_attempt(row) for row in rows)

    async def ensure_graph_thread_id(
        self,
        run_id: UUID,
        *,
        occurred_at: datetime,
    ) -> StoredInvestigationRun:
        """幂等地把业务运行绑定到由运行 ID 推导的 LangGraph thread。"""

        occurred_at = _normalize_time(occurred_at, field_name="occurred_at")
        expected_thread_id = build_graph_thread_id(run_id)
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(InvestigationRunRow)
                .where(InvestigationRunRow.id == run_id)
                .with_for_update()
            )
            if row is None:
                raise InvestigationRunNotFound(run_id)
            if occurred_at < row.updated_at:
                raise ValueError("graph_thread_id 绑定时间不能早于调查运行当前更新时间")
            if row.graph_thread_id is None:
                row.graph_thread_id = expected_thread_id
                row.updated_at = occurred_at
                await session.flush()
            elif row.graph_thread_id != expected_thread_id:
                raise GraphThreadIdMismatch(f"调查运行 {run_id} 已关联到不匹配的 graph_thread_id")
            return _stored_run(row)

    async def claim_next(
        self,
        *,
        worker_id: str,
        occurred_at: datetime,
        lease_duration: timedelta,
        data_source_mode: InvestigationDataSourceMode = InvestigationDataSourceMode.FROZEN,
        model_name: str | None = None,
        model_pricing: ModelPricing | None = None,
    ) -> ClaimedInvestigation | None:
        """跳过其他 Worker 已锁定任务，并接管到期租约。"""

        worker_id = _normalize_worker_id(worker_id)
        model_name = _normalize_optional_model_name(model_name)
        occurred_at = _normalize_time(occurred_at, field_name="occurred_at")
        if not timedelta(seconds=1) <= lease_duration <= timedelta(minutes=30):
            raise ValueError("lease_duration 必须在 1 秒到 30 分钟之间")

        async with self._session_factory() as session, session.begin():
            for _ in range(100):
                row = await session.scalar(
                    select(WorkItemRow)
                    .join(
                        InvestigationRunRow,
                        InvestigationRunRow.id == WorkItemRow.investigation_run_id,
                    )
                    .where(
                        InvestigationRunRow.data_source_mode == data_source_mode.value,
                        or_(
                            and_(
                                WorkItemRow.status == WorkItemStatus.PENDING.value,
                                WorkItemRow.available_at <= occurred_at,
                                WorkItemRow.attempt_count < WorkItemRow.max_attempts,
                            ),
                            and_(
                                WorkItemRow.status == WorkItemStatus.RUNNING.value,
                                WorkItemRow.locked_until <= occurred_at,
                            ),
                        ),
                    )
                    .order_by(WorkItemRow.available_at, WorkItemRow.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if row is None:
                    return None

                run_row = await _require_run(session, row.investigation_run_id)
                if row.status == WorkItemStatus.RUNNING.value:
                    await _expire_current_attempt(
                        session,
                        row,
                        occurred_at=occurred_at,
                        will_retry=row.attempt_count < row.max_attempts,
                    )
                if row.attempt_count >= row.max_attempts:
                    error = WorkItemError(
                        error_code="lease_expired_after_max_attempts",
                        message="最后一次处理租约到期，任务无法安全确认完成。",
                    )
                    _mark_terminal_failure(row, run_row, error=error, occurred_at=occurred_at)
                    continue

                lease_token = uuid4()
                lease_expires_at = occurred_at + lease_duration
                row.status = WorkItemStatus.RUNNING.value
                row.locked_by = worker_id
                row.locked_until = lease_expires_at
                row.lease_token = lease_token
                row.attempt_count += 1
                row.updated_at = occurred_at
                run_row.status = InvestigationRunStatus.RUNNING.value
                run_row.started_at = run_row.started_at or occurred_at
                run_row.updated_at = occurred_at

                incident_row = await session.get(IncidentRow, run_row.incident_id)
                if incident_row is None:
                    raise RuntimeError(f"调查运行关联的 Incident 不存在：{run_row.incident_id}")
                attempt_row = InvestigationAttemptRow(
                    id=uuid4(),
                    investigation_run_id=run_row.id,
                    work_item_id=row.id,
                    attempt_number=row.attempt_count,
                    lease_token=lease_token,
                    status=InvestigationAttemptStatus.RUNNING.value,
                    worker_id=worker_id,
                    model_name=model_name,
                    input_cost_per_million_usd=(
                        model_pricing.input_cost_per_million_usd
                        if model_pricing is not None
                        else None
                    ),
                    output_cost_per_million_usd=(
                        model_pricing.output_cost_per_million_usd
                        if model_pricing is not None
                        else None
                    ),
                    model_request_count=0,
                    usage_complete=False,
                    input_tokens=0,
                    output_tokens=0,
                    total_tokens=0,
                    estimated_cost_usd=None,
                    started_at=occurred_at,
                    lease_expires_at=lease_expires_at,
                    finished_at=None,
                    duration_ms=None,
                    will_retry=None,
                    error_code=None,
                    error_message=None,
                )
                session.add(attempt_row)
                await session.flush()
                return ClaimedInvestigation(
                    run=_stored_run(run_row),
                    work_item=_stored_work_item(row),
                    attempt=_stored_attempt(attempt_row),
                    request=InvestigationRequest(
                        incident_id=incident_row.id,
                        service_name=incident_row.service_name,
                        title=incident_row.title,
                        started_at=incident_row.started_at,
                    ),
                )

        raise RuntimeError("单次领取清理了过多耗尽任务，请再次轮询")

    async def complete(
        self,
        work_item_id: UUID,
        *,
        lease_token: UUID,
        occurred_at: datetime,
        result: InvestigationRunResult,
        evidence_items: tuple[EvidenceObservation, ...],
        specialist_reports: tuple[SpecialistReport, ...],
        hypothesis_challenge: HypothesisChallenge,
        metrics: InvestigationMetrics | None,
        model_name: str,
        usage: ModelUsageTelemetry | None = None,
    ) -> StoredInvestigationRun:
        """只有当前有效租约可以提交最终结构化结果。"""

        occurred_at = _normalize_time(occurred_at, field_name="occurred_at")
        model_name = model_name.strip()
        if not model_name or len(model_name) > 200:
            raise ValueError("model_name 长度必须在 1 到 200 之间")
        artifacts = InvestigationArtifacts(
            result=result,
            evidence_items=evidence_items,
            specialist_reports=specialist_reports,
        )
        validate_hypothesis_challenge(artifacts, hypothesis_challenge)
        usage = usage or ModelUsageTelemetry()

        async with self._session_factory() as session, session.begin():
            work_row = await _locked_work_item(session, work_item_id)
            _require_valid_lease(work_row, lease_token=lease_token, occurred_at=occurred_at)
            run_row = await _require_run(session, work_row.investigation_run_id)
            attempt_row = await _locked_attempt(
                session,
                work_item_id=work_item_id,
                lease_token=lease_token,
            )
            _finalize_attempt(
                attempt_row,
                status=InvestigationAttemptStatus.SUCCEEDED,
                occurred_at=occurred_at,
                usage=usage,
                will_retry=False,
                error=None,
                model_name=model_name,
            )

            work_row.status = WorkItemStatus.COMPLETED.value
            work_row.locked_by = None
            work_row.locked_until = None
            work_row.lease_token = None
            work_row.updated_at = occurred_at
            work_row.completed_at = occurred_at
            run_row.status = InvestigationRunStatus.SUCCEEDED.value
            run_row.model_name = model_name
            run_row.result = result.model_dump(mode="json")
            run_row.metrics = metrics.model_dump(mode="json") if metrics is not None else None
            run_row.last_error = None
            run_row.completed_at = occurred_at
            run_row.updated_at = occurred_at
            await _persist_evidence_board(
                session,
                run_id=run_row.id,
                artifacts=artifacts,
                hypothesis_challenge=hypothesis_challenge,
                created_at=occurred_at,
            )
            await session.flush()
            return _stored_run(run_row)

    async def fail(
        self,
        work_item_id: UUID,
        *,
        lease_token: UUID,
        occurred_at: datetime,
        error: WorkItemError,
        retry_delay: timedelta,
        usage: ModelUsageTelemetry | None = None,
    ) -> StoredWorkItem:
        """记录失败；有剩余次数则延迟重试，否则进入终态。"""

        occurred_at = _normalize_time(occurred_at, field_name="occurred_at")
        if not timedelta(0) <= retry_delay <= timedelta(hours=1):
            raise ValueError("retry_delay 必须在 0 到 1 小时之间")
        usage = usage or ModelUsageTelemetry()

        async with self._session_factory() as session, session.begin():
            work_row = await _locked_work_item(session, work_item_id)
            _require_valid_lease(work_row, lease_token=lease_token, occurred_at=occurred_at)
            run_row = await _require_run(session, work_row.investigation_run_id)
            will_retry = work_row.attempt_count < work_row.max_attempts
            attempt_row = await _locked_attempt(
                session,
                work_item_id=work_item_id,
                lease_token=lease_token,
            )
            _finalize_attempt(
                attempt_row,
                status=InvestigationAttemptStatus.FAILED,
                occurred_at=occurred_at,
                usage=usage,
                will_retry=will_retry,
                error=error,
                model_name=None,
            )
            if work_row.attempt_count >= work_row.max_attempts:
                _mark_terminal_failure(
                    work_row,
                    run_row,
                    error=error,
                    occurred_at=occurred_at,
                )
            else:
                error_payload = error.model_dump(mode="json")
                work_row.status = WorkItemStatus.PENDING.value
                work_row.available_at = occurred_at + retry_delay
                work_row.locked_by = None
                work_row.locked_until = None
                work_row.lease_token = None
                work_row.last_error = error_payload
                work_row.updated_at = occurred_at
                run_row.status = InvestigationRunStatus.QUEUED.value
                run_row.last_error = error_payload
                run_row.updated_at = occurred_at
            await session.flush()
            return _stored_work_item(work_row)


async def _persist_evidence_board(
    session: AsyncSession,
    *,
    run_id: UUID,
    artifacts: InvestigationArtifacts,
    hypothesis_challenge: HypothesisChallenge,
    created_at: datetime,
) -> None:
    """在调查完成事务中写入 Evidence Board、主假设和质疑关系。"""

    evidence_rows = {
        item.evidence_id: EvidenceItemRow(
            id=uuid5(run_id, f"evidence:{item.evidence_id}"),
            investigation_run_id=run_id,
            evidence_key=item.evidence_id,
            kind=item.kind.value,
            source=item.source,
            summary=item.summary,
            observed_at=item.observed_at,
            attributes=item.attributes,
            content_hash=_evidence_content_hash(item),
            created_at=created_at,
        )
        for item in artifacts.evidence_items
    }
    report_rows = {
        report.specialist: SpecialistReportRow(
            id=uuid5(run_id, f"specialist-report:{report.specialist.value}"),
            investigation_run_id=run_id,
            specialist=report.specialist.value,
            summary=report.summary,
            findings=list(report.findings),
            confidence=report.confidence,
            created_at=created_at,
        )
        for report in artifacts.specialist_reports
    }
    session.add_all([*evidence_rows.values(), *report_rows.values()])
    await session.flush()

    session.add_all(
        [
            SpecialistReportEvidenceRow(
                investigation_run_id=run_id,
                specialist_report_id=report_rows[report.specialist].id,
                evidence_item_id=evidence_rows[evidence_id].id,
            )
            for report in artifacts.specialist_reports
            for evidence_id in report.evidence_ids
        ]
    )

    hypothesis_row = HypothesisRow(
        id=uuid5(run_id, "hypothesis:primary"),
        investigation_run_id=run_id,
        root_cause=hypothesis_challenge.root_cause.value,
        statement=hypothesis_challenge.statement,
        confidence=hypothesis_challenge.confidence,
        status=hypothesis_challenge.status.value,
        challenge_summary=hypothesis_challenge.summary,
        rule_version=hypothesis_challenge.rule_version,
        created_at=created_at,
    )
    session.add(hypothesis_row)
    await session.flush()
    session.add_all(
        [
            HypothesisEvidenceRow(
                investigation_run_id=run_id,
                hypothesis_id=hypothesis_row.id,
                evidence_item_id=evidence_rows[item.evidence_id].id,
                relation=item.relation.value,
                rule_code=item.rule_code,
                rationale=item.rationale,
                created_at=created_at,
            )
            for item in hypothesis_challenge.evidence_assessments
        ]
    )


def _evidence_content_hash(item: EvidenceObservation) -> str:
    """对规范化内容计算稳定摘要，不把数据库标识或采集时间混入。"""

    canonical = json.dumps(
        item.model_dump(mode="json"),
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalize_time(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return value.astimezone(UTC)


def _normalize_worker_id(worker_id: str) -> str:
    normalized = worker_id.strip()
    if not normalized or len(normalized) > 100:
        raise ValueError("worker_id 长度必须在 1 到 100 之间")
    return normalized


def _normalize_optional_model_name(model_name: str | None) -> str | None:
    if model_name is None:
        return None
    normalized = model_name.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("model_name 长度必须在 1 到 200 之间")
    return normalized


async def _require_run(session: AsyncSession, run_id: UUID) -> InvestigationRunRow:
    row = await session.get(InvestigationRunRow, run_id)
    if row is None:
        raise InvestigationRunNotFound(run_id)
    return row


async def _locked_work_item(session: AsyncSession, work_item_id: UUID) -> WorkItemRow:
    row = await session.scalar(
        select(WorkItemRow).where(WorkItemRow.id == work_item_id).with_for_update()
    )
    if row is None:
        raise WorkItemNotFound(work_item_id)
    return row


async def _locked_attempt(
    session: AsyncSession,
    *,
    work_item_id: UUID,
    lease_token: UUID,
) -> InvestigationAttemptRow:
    row = await session.scalar(
        select(InvestigationAttemptRow)
        .where(
            InvestigationAttemptRow.work_item_id == work_item_id,
            InvestigationAttemptRow.lease_token == lease_token,
        )
        .with_for_update()
    )
    if row is None or row.status != InvestigationAttemptStatus.RUNNING.value:
        raise WorkItemLeaseLost(work_item_id)
    return row


async def _expire_current_attempt(
    session: AsyncSession,
    work_row: WorkItemRow,
    *,
    occurred_at: datetime,
    will_retry: bool,
) -> None:
    """在接管之前把上一个未结算租约收敛为过期。"""

    if work_row.lease_token is None:
        return
    row = await session.scalar(
        select(InvestigationAttemptRow)
        .where(
            InvestigationAttemptRow.work_item_id == work_row.id,
            InvestigationAttemptRow.lease_token == work_row.lease_token,
        )
        .with_for_update()
    )
    if row is None:
        # 允许迁移前已在运行的历史任务被接管。
        return
    error = WorkItemError(
        error_code="lease_expired",
        message="Worker 租约到期，未能确认本次调查结果。",
    )
    _finalize_attempt(
        row,
        status=InvestigationAttemptStatus.LEASE_EXPIRED,
        occurred_at=occurred_at,
        usage=ModelUsageTelemetry(),
        will_retry=will_retry,
        error=error,
        model_name=None,
    )


def _finalize_attempt(
    row: InvestigationAttemptRow,
    *,
    status: InvestigationAttemptStatus,
    occurred_at: datetime,
    usage: ModelUsageTelemetry,
    will_retry: bool,
    error: WorkItemError | None,
    model_name: str | None,
) -> None:
    """将运行态尝试只结算一次，并校验价格与费用形状。"""

    if row.status != InvestigationAttemptStatus.RUNNING.value:
        raise WorkItemLeaseLost(row.work_item_id)
    if occurred_at < row.started_at:
        raise ValueError("尝试结束时间不能早于开始时间")
    normalized_model_name = _normalize_optional_model_name(model_name)
    if row.model_name is None:
        row.model_name = normalized_model_name
    elif normalized_model_name is not None and row.model_name != normalized_model_name:
        raise ValueError("尝试使用的 model_name 与领取时快照不一致")

    has_pricing = row.input_cost_per_million_usd is not None
    if has_pricing != (row.output_cost_per_million_usd is not None):
        raise RuntimeError("调查尝试价格快照不完整")
    if usage.estimated_cost_usd is not None and not has_pricing:
        raise ValueError("未保存价格快照时不能写入估算费用")
    if (
        status is not InvestigationAttemptStatus.LEASE_EXPIRED
        and usage.usage_complete
        and has_pricing
        and usage.estimated_cost_usd is None
    ):
        raise ValueError("用量与价格均可用时必须写入估算费用")
    if usage.usage_complete and has_pricing:
        assert row.input_cost_per_million_usd is not None
        assert row.output_cost_per_million_usd is not None
        expected_cost = (
            (
                Decimal(usage.input_tokens) * row.input_cost_per_million_usd
                + Decimal(usage.output_tokens) * row.output_cost_per_million_usd
            )
            / Decimal(1_000_000)
        ).quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
        if usage.estimated_cost_usd != expected_cost:
            raise ValueError("估算费用与 Token 及价格快照重算结果不一致")

    row.status = status.value
    row.model_request_count = usage.model_request_count
    row.usage_complete = usage.usage_complete
    row.input_tokens = usage.input_tokens
    row.output_tokens = usage.output_tokens
    row.total_tokens = usage.total_tokens
    row.estimated_cost_usd = usage.estimated_cost_usd
    row.finished_at = occurred_at
    row.duration_ms = int((occurred_at - row.started_at).total_seconds() * 1000)
    row.will_retry = will_retry
    row.error_code = error.error_code if error is not None else None
    row.error_message = error.message if error is not None else None


def _require_valid_lease(
    row: WorkItemRow,
    *,
    lease_token: UUID,
    occurred_at: datetime,
) -> None:
    if (
        row.status != WorkItemStatus.RUNNING.value
        or row.lease_token != lease_token
        or row.locked_until is None
        or row.locked_until <= occurred_at
    ):
        raise WorkItemLeaseLost(row.id)


def _mark_terminal_failure(
    work_row: WorkItemRow,
    run_row: InvestigationRunRow,
    *,
    error: WorkItemError,
    occurred_at: datetime,
) -> None:
    error_payload = error.model_dump(mode="json")
    work_row.status = WorkItemStatus.FAILED.value
    work_row.locked_by = None
    work_row.locked_until = None
    work_row.lease_token = None
    work_row.last_error = error_payload
    work_row.updated_at = occurred_at
    work_row.completed_at = occurred_at
    run_row.status = InvestigationRunStatus.FAILED.value
    run_row.last_error = error_payload
    run_row.completed_at = occurred_at
    run_row.updated_at = occurred_at


def _stored_run(row: InvestigationRunRow) -> StoredInvestigationRun:
    return StoredInvestigationRun(
        id=row.id,
        incident_id=row.incident_id,
        mode=InvestigationMode(row.mode),
        data_source_mode=InvestigationDataSourceMode(row.data_source_mode),
        status=InvestigationRunStatus(row.status),
        prompt_version=row.prompt_version,
        graph_thread_id=row.graph_thread_id,
        model_name=row.model_name,
        result=(
            InvestigationRunResult.model_validate(row.result) if row.result is not None else None
        ),
        metrics=(
            InvestigationMetrics.model_validate(row.metrics) if row.metrics is not None else None
        ),
        last_error=WorkItemError.model_validate(row.last_error)
        if row.last_error is not None
        else None,
        created_at=row.created_at,
        started_at=row.started_at,
        completed_at=row.completed_at,
        updated_at=row.updated_at,
    )


def _stored_work_item(row: WorkItemRow) -> StoredWorkItem:
    return StoredWorkItem(
        id=row.id,
        investigation_run_id=row.investigation_run_id,
        kind=WorkItemKind(row.kind),
        status=WorkItemStatus(row.status),
        idempotency_key=row.idempotency_key,
        available_at=row.available_at,
        locked_by=row.locked_by,
        locked_until=row.locked_until,
        lease_token=row.lease_token,
        attempt_count=row.attempt_count,
        max_attempts=row.max_attempts,
        last_error=WorkItemError.model_validate(row.last_error)
        if row.last_error is not None
        else None,
        trace_context=(
            TraceContextCarrier.model_validate(row.trace_context)
            if row.trace_context is not None
            else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _stored_attempt(row: InvestigationAttemptRow) -> StoredInvestigationAttempt:
    pricing = (
        ModelPricing(
            input_cost_per_million_usd=row.input_cost_per_million_usd,
            output_cost_per_million_usd=row.output_cost_per_million_usd,
        )
        if row.input_cost_per_million_usd is not None
        and row.output_cost_per_million_usd is not None
        else None
    )
    return StoredInvestigationAttempt(
        id=row.id,
        investigation_run_id=row.investigation_run_id,
        work_item_id=row.work_item_id,
        attempt_number=row.attempt_number,
        status=InvestigationAttemptStatus(row.status),
        worker_id=row.worker_id,
        model_name=row.model_name,
        pricing=pricing,
        usage=ModelUsageTelemetry(
            model_request_count=row.model_request_count,
            usage_complete=row.usage_complete,
            input_tokens=row.input_tokens,
            output_tokens=row.output_tokens,
            total_tokens=row.total_tokens,
            estimated_cost_usd=row.estimated_cost_usd,
        ),
        started_at=row.started_at,
        lease_expires_at=row.lease_expires_at,
        finished_at=row.finished_at,
        duration_ms=row.duration_ms,
        will_retry=row.will_retry,
        error=(
            WorkItemError(error_code=row.error_code, message=row.error_message)
            if row.error_code is not None and row.error_message is not None
            else None
        ),
    )


def _stored_evidence_item(row: EvidenceItemRow) -> StoredEvidenceItem:
    return StoredEvidenceItem(
        id=row.id,
        investigation_run_id=row.investigation_run_id,
        evidence_id=row.evidence_key,
        kind=EvidenceKind(row.kind),
        source=row.source,
        summary=row.summary,
        observed_at=row.observed_at,
        attributes=row.attributes,
        content_hash=row.content_hash,
        created_at=row.created_at,
    )


def _stored_specialist_report(
    row: SpecialistReportRow,
    *,
    evidence_ids: tuple[str, ...],
) -> StoredSpecialistReport:
    return StoredSpecialistReport(
        id=row.id,
        investigation_run_id=row.investigation_run_id,
        specialist=EvidenceKind(row.specialist),
        summary=row.summary,
        findings=tuple(row.findings),
        evidence_ids=evidence_ids,
        confidence=row.confidence,
        created_at=row.created_at,
    )


def _stored_hypothesis_evidence(
    row: HypothesisEvidenceRow,
    *,
    evidence_id: str,
) -> StoredHypothesisEvidence:
    return StoredHypothesisEvidence(
        evidence_item_id=row.evidence_item_id,
        evidence_id=evidence_id,
        relation=EvidenceRelation(row.relation),
        rule_code=row.rule_code,
        rationale=row.rationale,
        created_at=row.created_at,
    )


def _stored_hypothesis(
    row: HypothesisRow,
    *,
    evidence_assessments: tuple[StoredHypothesisEvidence, ...],
) -> StoredHypothesis:
    return StoredHypothesis(
        id=row.id,
        investigation_run_id=row.investigation_run_id,
        root_cause=RootCause(row.root_cause),
        statement=row.statement,
        confidence=row.confidence,
        status=HypothesisStatus(row.status),
        challenge_summary=row.challenge_summary,
        rule_version=row.rule_version,
        evidence_assessments=evidence_assessments,
        created_at=row.created_at,
    )
