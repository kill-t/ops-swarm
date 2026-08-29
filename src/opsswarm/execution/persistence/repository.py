"""操作执行的 PostgreSQL 租约、幂等和状态迁移事务。"""

import re
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval import ApprovalRequestStatus
from opsswarm.approval.persistence import load_approval_request_in_session
from opsswarm.approval.persistence.tables import ApprovalRequestRow
from opsswarm.execution.errors import (
    ExecutionLeaseLost,
    OperationExecutionNotFound,
    OperationIdempotencyConflict,
    OperationNotAuthorized,
    OperationStepAlreadyRunning,
    OperationStepAlreadySucceeded,
    OperationStepNotFound,
)
from opsswarm.execution.models import ExecutionStatus, OperationFailure, OperationResult
from opsswarm.execution.persistence.records import (
    ClaimedOperationExecution,
    StoredOperationExecution,
)
from opsswarm.execution.persistence.tables import OperationExecutionRow
from opsswarm.incidents import ActorType, IncidentAction, IncidentActor, IncidentStatus
from opsswarm.incidents.errors import IncidentNotFound, IncidentVersionConflict
from opsswarm.incidents.persistence.repository import transition_incident_in_session
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.planning import ActionPlanStatus, ActionType
from opsswarm.planning.persistence import load_action_plan_in_session
from opsswarm.planning.persistence.tables import ActionPlanRow, ActionStepRow

RELEASE_EXECUTOR_NAME = "local_fault_lab_release_500_v1"
CONFIGURATION_EXECUTOR_NAME = "local_fault_lab_database_pool_v1"
_EXECUTABLE_ACTION_TYPES = frozenset(
    {
        ActionType.ROLLBACK_RELEASE,
        ActionType.ROLLBACK_CONFIGURATION,
    }
)
_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,99}$")


class OperationExecutionStore:
    """持久领取和完成白名单操作，支持崩溃后的同键恢复。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def claim(
        self,
        step_id: UUID,
        *,
        idempotency_key: str,
        expected_incident_version: int,
        occurred_at: datetime,
        lease_duration: timedelta,
    ) -> ClaimedOperationExecution:
        """领取一个步骤；终态或有效租约按幂等语义直接返回。"""

        idempotency_key = _normalize_idempotency_key(idempotency_key)
        occurred_at = _normalize_time(occurred_at)
        if lease_duration <= timedelta(0):
            raise ValueError("lease_duration 必须大于 0")

        async with self._session_factory() as session, session.begin():
            # 全局幂等键先取事务级咨询锁，避免不同步骤同时抢占同一个键。
            await session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": idempotency_key},
            )
            step_row = cast(
                ActionStepRow | None,
                await session.scalar(
                    select(ActionStepRow).where(ActionStepRow.id == step_id).with_for_update()
                ),
            )
            if step_row is None:
                raise OperationStepNotFound(step_id)
            plan_row = await session.get(ActionPlanRow, step_row.action_plan_id)
            if plan_row is None:
                raise RuntimeError("处置步骤关联的计划不存在")
            run_row = await session.get(InvestigationRunRow, plan_row.investigation_run_id)
            if run_row is None:
                raise RuntimeError("处置计划关联的调查运行不存在")
            incident_row = cast(
                IncidentRow | None,
                await session.scalar(
                    select(IncidentRow)
                    .where(IncidentRow.id == run_row.incident_id)
                    .with_for_update()
                ),
            )
            if incident_row is None:
                raise IncidentNotFound(str(run_row.incident_id))
            stored_plan = await load_action_plan_in_session(session, plan_row)
            stored_step = next(item for item in stored_plan.steps if item.id == step_id)

            existing = await session.scalar(
                select(OperationExecutionRow).where(
                    OperationExecutionRow.idempotency_key == idempotency_key
                )
            )
            if existing is not None:
                if existing.action_step_id != step_id:
                    raise OperationIdempotencyConflict(idempotency_key)
                stored_existing = _stored_execution(existing)
                if stored_existing.status is not ExecutionStatus.RUNNING:
                    return ClaimedOperationExecution(
                        execution=stored_existing,
                        step=stored_step,
                        claimed=False,
                        created=False,
                    )
                if existing.locked_until is not None and existing.locked_until > occurred_at:
                    return ClaimedOperationExecution(
                        execution=stored_existing,
                        step=stored_step,
                        claimed=False,
                        created=False,
                    )

            if existing is None:
                successful = await session.scalar(
                    select(OperationExecutionRow).where(
                        OperationExecutionRow.action_step_id == step_id,
                        OperationExecutionRow.status == ExecutionStatus.SUCCEEDED.value,
                    )
                )
                if successful is not None:
                    raise OperationStepAlreadySucceeded(step_id, execution_id=successful.id)
                running = await session.scalar(
                    select(OperationExecutionRow).where(
                        OperationExecutionRow.action_step_id == step_id,
                        OperationExecutionRow.status == ExecutionStatus.RUNNING.value,
                    )
                )
                if running is not None:
                    raise OperationStepAlreadyRunning(step_id, execution_id=running.id)

            approval_row = await _validate_authorization(
                session,
                step_id=step_id,
                plan_row=plan_row,
                incident_row=incident_row,
                expected_incident_version=expected_incident_version,
            )
            if stored_step.action_type not in _EXECUTABLE_ACTION_TYPES:
                raise OperationNotAuthorized(
                    step_id,
                    reason_code="action_type_not_executable",
                    message="当前确定性执行器尚未开放该动作类型",
                )
            if existing is not None:
                existing.lease_token = uuid4()
                existing.locked_until = occurred_at + lease_duration
                existing.attempt_count += 1
                existing.updated_at = occurred_at
                await session.flush()
                return ClaimedOperationExecution(
                    execution=_stored_execution(existing),
                    step=stored_step,
                    claimed=True,
                    created=False,
                )

            execution_row = OperationExecutionRow(
                id=uuid5(step_id, f"operation-execution:{idempotency_key}"),
                idempotency_key=idempotency_key,
                incident_id=run_row.incident_id,
                approval_request_id=approval_row.id,
                action_plan_id=plan_row.id,
                action_plan_version=plan_row.version,
                action_step_id=step_id,
                status=ExecutionStatus.RUNNING.value,
                executor_name=_executor_name(stored_step.action_type),
                incident_version_before=incident_row.version,
                attempt_count=1,
                lease_token=uuid4(),
                locked_until=occurred_at + lease_duration,
                result=None,
                error_code=None,
                error_message=None,
                created_at=occurred_at,
                updated_at=occurred_at,
                completed_at=None,
            )
            session.add(execution_row)
            await session.flush()
            return ClaimedOperationExecution(
                execution=_stored_execution(execution_row),
                step=stored_step,
                claimed=True,
                created=True,
            )

    async def complete(
        self,
        execution_id: UUID,
        *,
        lease_token: UUID,
        occurred_at: datetime,
        result: OperationResult | None = None,
        failure: OperationFailure | None = None,
    ) -> StoredOperationExecution:
        """用有效租约提交唯一终态，并同步迁移 Incident。"""

        occurred_at = _normalize_time(occurred_at)
        if (result is None) == (failure is None):
            raise ValueError("result 和 failure 必须且只能提供一个")
        async with self._session_factory() as session, session.begin():
            row = cast(
                OperationExecutionRow | None,
                await session.scalar(
                    select(OperationExecutionRow)
                    .where(OperationExecutionRow.id == execution_id)
                    .with_for_update()
                ),
            )
            if row is None:
                raise OperationExecutionNotFound(execution_id)
            if row.status != ExecutionStatus.RUNNING.value:
                return _stored_execution(row)
            if (
                row.lease_token != lease_token
                or row.locked_until is None
                or row.locked_until <= occurred_at
            ):
                raise ExecutionLeaseLost(execution_id)
            incident_row = cast(
                IncidentRow | None,
                await session.scalar(
                    select(IncidentRow).where(IncidentRow.id == row.incident_id).with_for_update()
                ),
            )
            if incident_row is None:
                raise IncidentNotFound(str(row.incident_id))
            occurred_at = max(occurred_at, incident_row.updated_at, row.updated_at)
            succeeded = result is not None
            await transition_incident_in_session(
                session,
                row.incident_id,
                action=(
                    IncidentAction.COMPLETE_MITIGATION
                    if succeeded
                    else IncidentAction.FAIL_MITIGATION
                ),
                expected_version=row.incident_version_before,
                occurred_at=occurred_at,
                actor=IncidentActor(
                    actor_type=ActorType.EXECUTOR,
                    actor_id=row.executor_name,
                ),
                reason=("白名单操作执行成功" if succeeded else "白名单操作执行失败"),
            )
            row.status = (
                ExecutionStatus.SUCCEEDED.value if succeeded else ExecutionStatus.FAILED.value
            )
            row.lease_token = None
            row.locked_until = None
            row.result = result.model_dump(mode="json") if result is not None else None
            row.error_code = failure.error_code if failure is not None else None
            row.error_message = failure.message if failure is not None else None
            row.updated_at = occurred_at
            row.completed_at = occurred_at
            await session.flush()
            return _stored_execution(row)

    async def get(self, execution_id: UUID) -> StoredOperationExecution:
        async with self._session_factory() as session:
            row = await session.get(OperationExecutionRow, execution_id)
            if row is None:
                raise OperationExecutionNotFound(execution_id)
            return _stored_execution(row)


async def _validate_authorization(
    session: AsyncSession,
    *,
    step_id: UUID,
    plan_row: ActionPlanRow,
    incident_row: IncidentRow,
    expected_incident_version: int,
) -> ApprovalRequestRow:
    if incident_row.version != expected_incident_version:
        raise IncidentVersionConflict(
            expected_version=expected_incident_version,
            actual_version=incident_row.version,
        )
    if incident_row.status != IncidentStatus.MITIGATING.value:
        raise OperationNotAuthorized(
            step_id,
            reason_code="incident_not_mitigating",
            message="Incident 不在 mitigating，不能执行处置步骤",
        )
    current_plan_id = await session.scalar(
        select(ActionPlanRow.id)
        .where(ActionPlanRow.investigation_run_id == plan_row.investigation_run_id)
        .order_by(ActionPlanRow.version.desc())
        .limit(1)
    )
    if current_plan_id != plan_row.id or plan_row.status != ActionPlanStatus.APPROVED.value:
        raise OperationNotAuthorized(
            step_id,
            reason_code="plan_not_approved_current_version",
            message="只有当前已批准计划可以执行",
        )
    approval_row = await session.scalar(
        select(ApprovalRequestRow).where(ApprovalRequestRow.action_plan_id == plan_row.id)
    )
    if approval_row is None:
        raise OperationNotAuthorized(
            step_id,
            reason_code="approval_missing",
            message="处置计划没有审批记录",
        )
    approval = await load_approval_request_in_session(session, approval_row)
    if (
        approval.status is not ApprovalRequestStatus.APPROVED
        or approval.action_plan_version != plan_row.version
    ):
        raise OperationNotAuthorized(
            step_id,
            reason_code="approval_not_valid",
            message="处置计划没有有效批准",
        )
    return approval_row


def _stored_execution(row: OperationExecutionRow) -> StoredOperationExecution:
    return StoredOperationExecution(
        id=row.id,
        idempotency_key=row.idempotency_key,
        incident_id=row.incident_id,
        approval_request_id=row.approval_request_id,
        action_plan_id=row.action_plan_id,
        action_plan_version=row.action_plan_version,
        action_step_id=row.action_step_id,
        status=ExecutionStatus(row.status),
        executor_name=row.executor_name,
        incident_version_before=row.incident_version_before,
        attempt_count=row.attempt_count,
        lease_token=row.lease_token,
        locked_until=row.locked_until,
        result=OperationResult.model_validate(row.result) if row.result is not None else None,
        failure=(
            OperationFailure(error_code=row.error_code, message=row.error_message)
            if row.error_code is not None and row.error_message is not None
            else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


def _executor_name(action_type: ActionType) -> str:
    if action_type is ActionType.ROLLBACK_RELEASE:
        return RELEASE_EXECUTOR_NAME
    if action_type is ActionType.ROLLBACK_CONFIGURATION:
        return CONFIGURATION_EXECUTOR_NAME
    raise ValueError(f"没有动作 {action_type.value} 的确定性执行器")


def _normalize_idempotency_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("idempotency_key 必须是字符串")
    normalized = value.strip()
    if _IDEMPOTENCY_PATTERN.fullmatch(normalized) is None:
        raise ValueError("idempotency_key 格式无效")
    return normalized


def _normalize_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at 必须包含时区")
    return value.astimezone(UTC)
