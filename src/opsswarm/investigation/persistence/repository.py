"""调查运行与 PostgreSQL 租约任务仓储。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.incidents import IncidentAction, IncidentActor
from opsswarm.incidents.persistence.repository import transition_incident_in_session
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.errors import (
    GraphThreadIdMismatch,
    InvestigationRunNotFound,
    WorkItemLeaseLost,
    WorkItemNotFound,
)
from opsswarm.investigation.graph_thread import build_graph_thread_id
from opsswarm.investigation.models import (
    InvestigationMetrics,
    InvestigationMode,
    InvestigationRequest,
    InvestigationRunResult,
    InvestigationRunStatus,
    WorkItemError,
    WorkItemKind,
    WorkItemStatus,
)
from opsswarm.investigation.persistence.records import (
    ClaimedInvestigation,
    StartedInvestigation,
    StoredInvestigationRun,
    StoredWorkItem,
)
from opsswarm.investigation.persistence.tables import InvestigationRunRow, WorkItemRow

DEFAULT_PROMPT_VERSION = "single-agent-v1"
MULTI_AGENT_PROMPT_VERSION = "multi-agent-parallel-v1"

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

    async def get_work_item(self, work_item_id: UUID) -> StoredWorkItem:
        """读取一个持久任务。"""

        async with self._session_factory() as session:
            row = await session.get(WorkItemRow, work_item_id)
            if row is None:
                raise WorkItemNotFound(work_item_id)
            return _stored_work_item(row)

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
    ) -> ClaimedInvestigation | None:
        """跳过其他 Worker 已锁定任务，并接管到期租约。"""

        worker_id = _normalize_worker_id(worker_id)
        occurred_at = _normalize_time(occurred_at, field_name="occurred_at")
        if not timedelta(seconds=1) <= lease_duration <= timedelta(minutes=30):
            raise ValueError("lease_duration 必须在 1 秒到 30 分钟之间")

        async with self._session_factory() as session, session.begin():
            for _ in range(100):
                row = await session.scalar(
                    select(WorkItemRow)
                    .where(
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
                        )
                    )
                    .order_by(WorkItemRow.available_at, WorkItemRow.created_at)
                    .with_for_update(skip_locked=True)
                    .limit(1)
                )
                if row is None:
                    return None

                run_row = await _require_run(session, row.investigation_run_id)
                if row.attempt_count >= row.max_attempts:
                    error = WorkItemError(
                        error_code="lease_expired_after_max_attempts",
                        message="最后一次处理租约到期，任务无法安全确认完成。",
                    )
                    _mark_terminal_failure(row, run_row, error=error, occurred_at=occurred_at)
                    continue

                row.status = WorkItemStatus.RUNNING.value
                row.locked_by = worker_id
                row.locked_until = occurred_at + lease_duration
                row.lease_token = uuid4()
                row.attempt_count += 1
                row.updated_at = occurred_at
                run_row.status = InvestigationRunStatus.RUNNING.value
                run_row.started_at = run_row.started_at or occurred_at
                run_row.updated_at = occurred_at

                incident_row = await session.get(IncidentRow, run_row.incident_id)
                if incident_row is None:
                    raise RuntimeError(f"调查运行关联的 Incident 不存在：{run_row.incident_id}")
                await session.flush()
                return ClaimedInvestigation(
                    run=_stored_run(run_row),
                    work_item=_stored_work_item(row),
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
        metrics: InvestigationMetrics,
        model_name: str,
    ) -> StoredInvestigationRun:
        """只有当前有效租约可以提交最终结构化结果。"""

        occurred_at = _normalize_time(occurred_at, field_name="occurred_at")
        model_name = model_name.strip()
        if not model_name or len(model_name) > 200:
            raise ValueError("model_name 长度必须在 1 到 200 之间")

        async with self._session_factory() as session, session.begin():
            work_row = await _locked_work_item(session, work_item_id)
            _require_valid_lease(work_row, lease_token=lease_token, occurred_at=occurred_at)
            run_row = await _require_run(session, work_row.investigation_run_id)

            work_row.status = WorkItemStatus.COMPLETED.value
            work_row.locked_by = None
            work_row.locked_until = None
            work_row.lease_token = None
            work_row.updated_at = occurred_at
            work_row.completed_at = occurred_at
            run_row.status = InvestigationRunStatus.SUCCEEDED.value
            run_row.model_name = model_name
            run_row.result = result.model_dump(mode="json")
            run_row.metrics = metrics.model_dump(mode="json")
            run_row.last_error = None
            run_row.completed_at = occurred_at
            run_row.updated_at = occurred_at
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
    ) -> StoredWorkItem:
        """记录失败；有剩余次数则延迟重试，否则进入终态。"""

        occurred_at = _normalize_time(occurred_at, field_name="occurred_at")
        if not timedelta(0) <= retry_delay <= timedelta(hours=1):
            raise ValueError("retry_delay 必须在 0 到 1 小时之间")

        async with self._session_factory() as session, session.begin():
            work_row = await _locked_work_item(session, work_item_id)
            _require_valid_lease(work_row, lease_token=lease_token, occurred_at=occurred_at)
            run_row = await _require_run(session, work_row.investigation_run_id)
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


def _normalize_time(value: datetime, *, field_name: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} 必须包含时区")
    return value.astimezone(UTC)


def _normalize_worker_id(worker_id: str) -> str:
    normalized = worker_id.strip()
    if not normalized or len(normalized) > 100:
        raise ValueError("worker_id 长度必须在 1 到 100 之间")
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
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )
