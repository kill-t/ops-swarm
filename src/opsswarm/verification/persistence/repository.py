"""恢复验证运行、不可变样本和 Incident 结论事务。"""

import re
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4, uuid5

from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.execution import ExecutionStatus, OperationExecutionNotFound
from opsswarm.execution.persistence.tables import OperationExecutionRow
from opsswarm.incidents import ActorType, IncidentAction, IncidentActor, IncidentStatus
from opsswarm.incidents.errors import IncidentNotFound, IncidentVersionConflict
from opsswarm.incidents.persistence.repository import transition_incident_in_session
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.planning import ActionType, TargetType, VerificationOperator, VerificationRule
from opsswarm.planning.persistence import StoredActionStep, load_action_plan_in_session
from opsswarm.planning.persistence.tables import ActionPlanRow, ActionStepRow
from opsswarm.verification.errors import (
    OperationExecutionNotVerifiable,
    UnsupportedVerificationRule,
    VerificationRunCompleted,
    VerificationRunNotFound,
    VerificationWorkItemLeaseLost,
    VerificationWorkItemNotFound,
)
from opsswarm.verification.models import (
    VerificationFailure,
    VerificationObservation,
    VerificationSampleStatus,
    VerificationStatus,
    VerificationWorkItemError,
    VerificationWorkItemStatus,
    sample_status,
)
from opsswarm.verification.persistence.records import (
    PreparedVerificationSample,
    StoredVerificationRun,
    StoredVerificationSample,
    StoredVerificationWorkItem,
    VerificationRunCreation,
    scheduled_sample_idempotency_key,
)
from opsswarm.verification.persistence.tables import (
    VerificationRunRow,
    VerificationSampleRow,
    VerificationWorkItemRow,
)

RELEASE_VERIFIER_NAME = "local_order_http_5xx_ratio_v1"
CONFIGURATION_VERIFIER_NAME = "local_database_pool_saturation_v1"
REDIS_VERIFIER_NAME = "local_redis_unavailable_ratio_v1"
_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,99}$")
_WORKER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{2,99}$")


class VerificationStore:
    """持久化逐次恢复采样，并在终态原子迁移 Incident。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_for_execution(
        self,
        execution_id: UUID,
        *,
        expected_incident_version: int,
        occurred_at: datetime,
    ) -> VerificationRunCreation:
        """为成功操作创建唯一验证运行；重复请求返回原运行。"""

        occurred_at = _normalize_time(occurred_at)
        async with self._session_factory() as session, session.begin():
            execution_row = cast(
                OperationExecutionRow | None,
                await session.scalar(
                    select(OperationExecutionRow)
                    .where(OperationExecutionRow.id == execution_id)
                    .with_for_update()
                ),
            )
            if execution_row is None:
                raise OperationExecutionNotFound(execution_id)
            existing = await session.scalar(
                select(VerificationRunRow).where(
                    VerificationRunRow.operation_execution_id == execution_id
                )
            )
            if existing is not None:
                return VerificationRunCreation(
                    run=await _load_stored_run(session, existing),
                    created=False,
                )
            if execution_row.status != ExecutionStatus.SUCCEEDED.value:
                raise OperationExecutionNotVerifiable(
                    execution_id,
                    reason_code="execution_not_succeeded",
                    message="只有成功操作可以开始恢复验证",
                )
            incident_row = cast(
                IncidentRow | None,
                await session.scalar(
                    select(IncidentRow)
                    .where(IncidentRow.id == execution_row.incident_id)
                    .with_for_update()
                ),
            )
            if incident_row is None:
                raise IncidentNotFound(str(execution_row.incident_id))
            if incident_row.version != expected_incident_version:
                raise IncidentVersionConflict(
                    expected_version=expected_incident_version,
                    actual_version=incident_row.version,
                )
            if incident_row.status != IncidentStatus.VERIFYING.value:
                raise OperationExecutionNotVerifiable(
                    execution_id,
                    reason_code="incident_not_verifying",
                    message="Incident 不在 verifying，不能开始恢复验证",
                )
            step = await _load_verified_step(
                session,
                execution_row.action_step_id,
                expected_plan_id=execution_row.action_plan_id,
            )
            _validate_supported_rule(execution_id, step)
            run_row = VerificationRunRow(
                id=uuid5(execution_id, "verification-run"),
                operation_execution_id=execution_id,
                incident_id=execution_row.incident_id,
                action_step_id=execution_row.action_step_id,
                status=VerificationStatus.RUNNING.value,
                verifier_name=_verifier_name(step),
                incident_version_before=incident_row.version,
                signal=step.verification.signal,
                operator=step.verification.operator.value,
                threshold=step.verification.threshold,
                observation_window_seconds=step.verification.observation_window_seconds,
                required_successful_samples=step.verification.required_successful_samples,
                successful_sample_count=0,
                first_success_at=None,
                last_observed_at=None,
                failure_code=None,
                failure_message=None,
                created_at=max(occurred_at, incident_row.updated_at),
                updated_at=max(occurred_at, incident_row.updated_at),
                completed_at=None,
            )
            session.add(run_row)
            session.add(
                VerificationWorkItemRow(
                    id=run_row.id,
                    verification_run_id=run_row.id,
                    status=VerificationWorkItemStatus.PENDING.value,
                    next_sample_sequence=1,
                    available_at=run_row.created_at,
                    locked_by=None,
                    locked_until=None,
                    lease_token=None,
                    attempt_count=0,
                    consecutive_failure_count=0,
                    last_error_code=None,
                    last_error_message=None,
                    created_at=run_row.created_at,
                    updated_at=run_row.created_at,
                    completed_at=None,
                )
            )
            await session.flush()
            return VerificationRunCreation(
                run=await _load_stored_run(session, run_row),
                created=True,
            )

    async def prepare_sample(
        self,
        run_id: UUID,
        *,
        idempotency_key: str,
    ) -> PreparedVerificationSample:
        """在只读探针调用前处理同键幂等和终态拒绝。"""

        idempotency_key = _normalize_idempotency_key(idempotency_key)
        async with self._session_factory() as session:
            run_row = await session.get(VerificationRunRow, run_id)
            if run_row is None:
                raise VerificationRunNotFound(run_id)
            run = await _load_stored_run(session, run_row)
            existing = next(
                (item for item in run.samples if item.idempotency_key == idempotency_key),
                None,
            )
            if existing is None and run.status is not VerificationStatus.RUNNING:
                raise VerificationRunCompleted(run_id, status=run.status.value)
            step = await _load_verified_step(
                session,
                run.action_step_id,
                expected_plan_id=None,
            )
            _validate_supported_rule(run.operation_execution_id, step)
            return PreparedVerificationSample(
                run=run,
                step=step,
                existing_sample=existing,
            )

    async def record_sample(
        self,
        run_id: UUID,
        *,
        idempotency_key: str,
        observed_at: datetime,
        observation: VerificationObservation | None = None,
        failure: VerificationFailure | None = None,
    ) -> tuple[StoredVerificationRun, StoredVerificationSample, bool]:
        """追加一个样本；达到终态时同步迁移 Incident。"""

        idempotency_key = _normalize_idempotency_key(idempotency_key)
        observed_at = _normalize_time(observed_at)
        if (observation is None) == (failure is None):
            raise ValueError("observation 和 failure 必须且只能提供一个")
        async with self._session_factory() as session, session.begin():
            run_row = cast(
                VerificationRunRow | None,
                await session.scalar(
                    select(VerificationRunRow)
                    .where(VerificationRunRow.id == run_id)
                    .with_for_update()
                ),
            )
            if run_row is None:
                raise VerificationRunNotFound(run_id)
            existing = await session.scalar(
                select(VerificationSampleRow).where(
                    VerificationSampleRow.verification_run_id == run_id,
                    VerificationSampleRow.idempotency_key == idempotency_key,
                )
            )
            if existing is not None:
                return (
                    await _load_stored_run(session, run_row),
                    _stored_sample(existing),
                    False,
                )
            if run_row.status != VerificationStatus.RUNNING.value:
                raise VerificationRunCompleted(run_id, status=run_row.status)
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
            if incident_row.version != run_row.incident_version_before:
                raise IncidentVersionConflict(
                    expected_version=run_row.incident_version_before,
                    actual_version=incident_row.version,
                )
            if incident_row.status != IncidentStatus.VERIFYING.value:
                raise OperationExecutionNotVerifiable(
                    run_row.operation_execution_id,
                    reason_code="incident_not_verifying",
                    message="Incident 已离开 verifying，不能追加恢复样本",
                )
            observed_at = max(observed_at, run_row.updated_at, incident_row.updated_at)
            sequence = (
                cast(
                    int,
                    (
                        await session.scalar(
                            select(func.count())
                            .select_from(VerificationSampleRow)
                            .where(VerificationSampleRow.verification_run_id == run_id)
                        )
                    )
                    or 0,
                )
                + 1
            )
            rule = _rule_from_row(run_row)
            sample_id = uuid5(run_id, f"verification-sample:{idempotency_key}")
            terminal_failure: VerificationFailure | None
            if failure is not None:
                sample_row = VerificationSampleRow(
                    id=sample_id,
                    verification_run_id=run_id,
                    idempotency_key=idempotency_key,
                    sequence=sequence,
                    status=VerificationSampleStatus.ERROR.value,
                    signal=rule.signal,
                    observed_value=None,
                    threshold=rule.threshold,
                    http_status=failure.http_status,
                    error_code=failure.error_code,
                    error_message=failure.message,
                    observed_at=observed_at,
                )
                terminal_failure = failure
            else:
                if observation is None:
                    raise RuntimeError("数值样本缺少 observation")
                status = sample_status(rule, observation)
                sample_row = VerificationSampleRow(
                    id=sample_id,
                    verification_run_id=run_id,
                    idempotency_key=idempotency_key,
                    sequence=sequence,
                    status=status.value,
                    signal=observation.signal,
                    observed_value=observation.value,
                    threshold=rule.threshold,
                    http_status=observation.http_status,
                    error_code=None,
                    error_message=None,
                    observed_at=observed_at,
                )
                terminal_failure = (
                    VerificationFailure(
                        error_code="recovery_threshold_not_met",
                        message=(
                            f"观测到 {rule.signal}={observation.value:g}，"
                            f"未满足小于 {rule.threshold:g}"
                        ),
                        http_status=observation.http_status,
                    )
                    if status is VerificationSampleStatus.FAILED
                    else None
                )
            session.add(sample_row)
            run_row.last_observed_at = observed_at
            if sample_row.status == VerificationSampleStatus.PASSED.value:
                run_row.successful_sample_count += 1
                if run_row.first_success_at is None:
                    run_row.first_success_at = observed_at
            run_row.updated_at = observed_at

            passed = _window_is_satisfied(run_row)
            if terminal_failure is not None:
                await _complete_run(
                    session,
                    run_row,
                    incident_row,
                    status=VerificationStatus.FAILED,
                    observed_at=observed_at,
                    failure=terminal_failure,
                )
            elif passed:
                await _complete_run(
                    session,
                    run_row,
                    incident_row,
                    status=VerificationStatus.PASSED,
                    observed_at=observed_at,
                    failure=None,
                )
            await session.flush()
            return (
                await _load_stored_run(session, run_row),
                _stored_sample(sample_row),
                True,
            )

    async def get(self, run_id: UUID) -> StoredVerificationRun:
        async with self._session_factory() as session:
            row = await session.get(VerificationRunRow, run_id)
            if row is None:
                raise VerificationRunNotFound(run_id)
            return await _load_stored_run(session, row)

    async def get_work_item(self, run_id: UUID) -> StoredVerificationWorkItem:
        """读取验证运行唯一的调度任务，不暴露为可修改对象。"""

        async with self._session_factory() as session:
            row = await session.scalar(
                select(VerificationWorkItemRow).where(
                    VerificationWorkItemRow.verification_run_id == run_id
                )
            )
            if row is None:
                raise VerificationWorkItemNotFound(run_id)
            return _stored_work_item(row)

    async def claim_next_work_item(
        self,
        *,
        worker_id: str,
        occurred_at: datetime,
        lease_duration: timedelta,
    ) -> StoredVerificationWorkItem | None:
        """并发安全领取一个到期任务，并接管已经过期的租约。"""

        worker_id = _normalize_worker_id(worker_id)
        occurred_at = _normalize_time(occurred_at)
        if not timedelta(seconds=1) <= lease_duration <= timedelta(minutes=5):
            raise ValueError("lease_duration 必须在 1 秒到 5 分钟之间")
        async with self._session_factory() as session, session.begin():
            row = await session.scalar(
                select(VerificationWorkItemRow)
                .where(
                    or_(
                        and_(
                            VerificationWorkItemRow.status
                            == VerificationWorkItemStatus.PENDING.value,
                            VerificationWorkItemRow.available_at <= occurred_at,
                        ),
                        and_(
                            VerificationWorkItemRow.status
                            == VerificationWorkItemStatus.RUNNING.value,
                            VerificationWorkItemRow.locked_until <= occurred_at,
                        ),
                    )
                )
                .order_by(
                    VerificationWorkItemRow.available_at,
                    VerificationWorkItemRow.created_at,
                )
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if row is None:
                return None
            row.status = VerificationWorkItemStatus.RUNNING.value
            row.locked_by = worker_id
            row.locked_until = occurred_at + lease_duration
            row.lease_token = uuid4()
            row.attempt_count += 1
            row.updated_at = occurred_at
            await session.flush()
            return _stored_work_item(row)

    async def settle_work_item(
        self,
        work_item_id: UUID,
        *,
        lease_token: UUID,
        occurred_at: datetime,
    ) -> StoredVerificationWorkItem:
        """确认同键样本已落库，并完成任务或安排下一次采样。"""

        occurred_at = _normalize_time(occurred_at)
        async with self._session_factory() as session, session.begin():
            work_row = await _locked_work_item(session, work_item_id)
            _require_valid_work_lease(
                work_row,
                lease_token=lease_token,
                occurred_at=occurred_at,
            )
            run_row = cast(
                VerificationRunRow | None,
                await session.scalar(
                    select(VerificationRunRow)
                    .where(VerificationRunRow.id == work_row.verification_run_id)
                    .with_for_update()
                ),
            )
            if run_row is None:
                raise VerificationRunNotFound(work_row.verification_run_id)
            if run_row.status != VerificationStatus.RUNNING.value:
                _complete_work_item(work_row, occurred_at=max(occurred_at, run_row.updated_at))
                await session.flush()
                return _stored_work_item(work_row)

            idempotency_key = scheduled_sample_idempotency_key(
                work_row.id,
                work_row.next_sample_sequence,
            )
            sample_row = await session.scalar(
                select(VerificationSampleRow).where(
                    VerificationSampleRow.verification_run_id == run_row.id,
                    VerificationSampleRow.idempotency_key == idempotency_key,
                )
            )
            if sample_row is None:
                raise RuntimeError("调度任务对应的恢复样本尚未持久化")
            if sample_row.status != VerificationSampleStatus.PASSED.value:
                raise RuntimeError("失败或错误样本必须已经使验证运行进入终态")

            settled_at = max(occurred_at, run_row.updated_at, sample_row.observed_at)
            work_row.status = VerificationWorkItemStatus.PENDING.value
            work_row.next_sample_sequence += 1
            work_row.available_at = max(
                settled_at,
                _next_sample_at(run_row),
            )
            work_row.locked_by = None
            work_row.locked_until = None
            work_row.lease_token = None
            work_row.consecutive_failure_count = 0
            work_row.updated_at = settled_at
            await session.flush()
            return _stored_work_item(work_row)

    async def retry_work_item(
        self,
        work_item_id: UUID,
        *,
        lease_token: UUID,
        occurred_at: datetime,
        error: VerificationWorkItemError,
        retry_delay: timedelta,
    ) -> StoredVerificationWorkItem:
        """保存有限错误并延迟重试，不因 Worker 故障伪造恢复失败。"""

        occurred_at = _normalize_time(occurred_at)
        if not timedelta(0) <= retry_delay <= timedelta(hours=1):
            raise ValueError("retry_delay 必须在 0 到 1 小时之间")
        async with self._session_factory() as session, session.begin():
            row = await _locked_work_item(session, work_item_id)
            _require_valid_work_lease(
                row,
                lease_token=lease_token,
                occurred_at=occurred_at,
            )
            row.status = VerificationWorkItemStatus.PENDING.value
            row.available_at = occurred_at + retry_delay
            row.locked_by = None
            row.locked_until = None
            row.lease_token = None
            row.consecutive_failure_count += 1
            row.last_error_code = error.error_code
            row.last_error_message = error.message
            row.updated_at = occurred_at
            await session.flush()
            return _stored_work_item(row)


async def _complete_run(
    session: AsyncSession,
    run_row: VerificationRunRow,
    incident_row: IncidentRow,
    *,
    status: VerificationStatus,
    observed_at: datetime,
    failure: VerificationFailure | None,
) -> None:
    passed = status is VerificationStatus.PASSED
    await transition_incident_in_session(
        session,
        run_row.incident_id,
        action=(IncidentAction.CONFIRM_RECOVERY if passed else IncidentAction.FAIL_VERIFICATION),
        expected_version=run_row.incident_version_before,
        occurred_at=observed_at,
        actor=IncidentActor(actor_type=ActorType.EXECUTOR, actor_id=run_row.verifier_name),
        reason="恢复验证通过" if passed else "恢复验证失败，继续调查",
    )
    run_row.status = status.value
    run_row.failure_code = failure.error_code if failure is not None else None
    run_row.failure_message = failure.message if failure is not None else None
    run_row.completed_at = observed_at


def _window_is_satisfied(row: VerificationRunRow) -> bool:
    if row.successful_sample_count < row.required_successful_samples:
        return False
    if row.first_success_at is None or row.last_observed_at is None:
        return False
    return row.last_observed_at - row.first_success_at >= timedelta(
        seconds=row.observation_window_seconds
    )


async def _load_verified_step(
    session: AsyncSession,
    step_id: UUID,
    *,
    expected_plan_id: UUID | None,
) -> StoredActionStep:
    step_row = await session.get(ActionStepRow, step_id)
    if step_row is None:
        raise RuntimeError("恢复验证关联的处置步骤不存在")
    if expected_plan_id is not None and step_row.action_plan_id != expected_plan_id:
        raise RuntimeError("恢复验证步骤与执行计划不一致")
    plan_row = await session.get(ActionPlanRow, step_row.action_plan_id)
    if plan_row is None:
        raise RuntimeError("恢复验证关联的处置计划不存在")
    plan = await load_action_plan_in_session(session, plan_row)
    return next(item for item in plan.steps if item.id == step_id)


def _validate_supported_rule(execution_id: UUID, step: StoredActionStep) -> None:
    rule = step.verification
    expected_contract = {
        ActionType.ROLLBACK_RELEASE: (
            TargetType.SERVICE,
            "order-service",
            "http_5xx_ratio",
        ),
        ActionType.ROLLBACK_CONFIGURATION: (
            TargetType.SERVICE,
            "order-service",
            "database_pool_saturation",
        ),
        ActionType.RESTORE_DEPENDENCY: (
            TargetType.DEPENDENCY,
            "redis",
            "redis_unavailable_ratio",
        ),
    }.get(step.action_type)
    if expected_contract is None:
        raise OperationExecutionNotVerifiable(
            execution_id,
            reason_code="verification_action_not_whitelisted",
            message="当前恢复验证不支持该动作类型",
        )
    expected_target_type, expected_target_name, expected_signal = expected_contract
    if step.target_type is not expected_target_type or step.target_name != expected_target_name:
        raise OperationExecutionNotVerifiable(
            execution_id,
            reason_code="verification_target_not_whitelisted",
            message="恢复验证步骤目标不在白名单中",
        )
    if rule.signal != expected_signal or rule.operator is not VerificationOperator.LESS_THAN:
        raise UnsupportedVerificationRule(
            signal=rule.signal,
            operator=rule.operator.value,
        )


def _verifier_name(step: StoredActionStep) -> str:
    if step.action_type is ActionType.ROLLBACK_RELEASE:
        return RELEASE_VERIFIER_NAME
    if step.action_type is ActionType.ROLLBACK_CONFIGURATION:
        return CONFIGURATION_VERIFIER_NAME
    if step.action_type is ActionType.RESTORE_DEPENDENCY:
        return REDIS_VERIFIER_NAME
    raise ValueError(f"没有动作 {step.action_type.value} 的恢复验证器")


async def _load_stored_run(
    session: AsyncSession,
    row: VerificationRunRow,
) -> StoredVerificationRun:
    sample_rows = tuple(
        (
            await session.scalars(
                select(VerificationSampleRow)
                .where(VerificationSampleRow.verification_run_id == row.id)
                .order_by(VerificationSampleRow.sequence)
            )
        ).all()
    )
    samples = tuple(_stored_sample(item) for item in sample_rows)
    return StoredVerificationRun(
        id=row.id,
        operation_execution_id=row.operation_execution_id,
        incident_id=row.incident_id,
        action_step_id=row.action_step_id,
        status=VerificationStatus(row.status),
        verifier_name=row.verifier_name,
        incident_version_before=row.incident_version_before,
        rule=_rule_from_row(row),
        successful_sample_count=row.successful_sample_count,
        first_success_at=row.first_success_at,
        last_observed_at=row.last_observed_at,
        failure=(
            VerificationFailure(error_code=row.failure_code, message=row.failure_message)
            if row.failure_code is not None and row.failure_message is not None
            else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
        samples=samples,
    )


def _stored_sample(row: VerificationSampleRow) -> StoredVerificationSample:
    return StoredVerificationSample(
        id=row.id,
        verification_run_id=row.verification_run_id,
        idempotency_key=row.idempotency_key,
        sequence=row.sequence,
        status=VerificationSampleStatus(row.status),
        signal=row.signal,
        observed_value=row.observed_value,
        threshold=row.threshold,
        http_status=row.http_status,
        failure=(
            VerificationFailure(
                error_code=row.error_code,
                message=row.error_message,
                http_status=row.http_status,
            )
            if row.error_code is not None and row.error_message is not None
            else None
        ),
        observed_at=row.observed_at,
    )


def _stored_work_item(row: VerificationWorkItemRow) -> StoredVerificationWorkItem:
    return StoredVerificationWorkItem(
        id=row.id,
        verification_run_id=row.verification_run_id,
        status=VerificationWorkItemStatus(row.status),
        next_sample_sequence=row.next_sample_sequence,
        available_at=row.available_at,
        locked_by=row.locked_by,
        locked_until=row.locked_until,
        lease_token=row.lease_token,
        attempt_count=row.attempt_count,
        consecutive_failure_count=row.consecutive_failure_count,
        last_error=(
            VerificationWorkItemError(
                error_code=row.last_error_code,
                message=row.last_error_message,
            )
            if row.last_error_code is not None and row.last_error_message is not None
            else None
        ),
        created_at=row.created_at,
        updated_at=row.updated_at,
        completed_at=row.completed_at,
    )


async def _locked_work_item(
    session: AsyncSession,
    work_item_id: UUID,
) -> VerificationWorkItemRow:
    row = cast(
        VerificationWorkItemRow | None,
        await session.scalar(
            select(VerificationWorkItemRow)
            .where(VerificationWorkItemRow.id == work_item_id)
            .with_for_update()
        ),
    )
    if row is None:
        raise VerificationWorkItemNotFound(work_item_id)
    return row


def _require_valid_work_lease(
    row: VerificationWorkItemRow,
    *,
    lease_token: UUID,
    occurred_at: datetime,
) -> None:
    if (
        row.status != VerificationWorkItemStatus.RUNNING.value
        or row.lease_token != lease_token
        or row.locked_until is None
        or row.locked_until <= occurred_at
    ):
        raise VerificationWorkItemLeaseLost(row.id)


def _complete_work_item(row: VerificationWorkItemRow, *, occurred_at: datetime) -> None:
    row.status = VerificationWorkItemStatus.COMPLETED.value
    row.locked_by = None
    row.locked_until = None
    row.lease_token = None
    row.updated_at = occurred_at
    row.completed_at = occurred_at


def _next_sample_at(row: VerificationRunRow) -> datetime:
    if row.first_success_at is None or row.successful_sample_count < 1:
        raise RuntimeError("运行中的调度任务缺少首个成功样本")
    window_seconds = row.observation_window_seconds
    if row.successful_sample_count >= row.required_successful_samples:
        return row.first_success_at + timedelta(seconds=window_seconds)
    divisor = max(row.required_successful_samples - 1, 1)
    interval_seconds = (window_seconds + divisor - 1) // divisor
    return row.first_success_at + timedelta(seconds=interval_seconds * row.successful_sample_count)


def _rule_from_row(row: VerificationRunRow) -> VerificationRule:
    return VerificationRule(
        signal=row.signal,
        operator=VerificationOperator(row.operator),
        threshold=row.threshold,
        observation_window_seconds=row.observation_window_seconds,
        required_successful_samples=row.required_successful_samples,
    )


def _normalize_idempotency_key(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("idempotency_key 必须是字符串")
    normalized = value.strip()
    if _IDEMPOTENCY_PATTERN.fullmatch(normalized) is None:
        raise ValueError("idempotency_key 格式无效")
    return normalized


def _normalize_worker_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("worker_id 必须是字符串")
    normalized = value.strip()
    if _WORKER_ID_PATTERN.fullmatch(normalized) is None:
        raise ValueError("worker_id 格式无效")
    return normalized


def _normalize_time(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("occurred_at 必须包含时区")
    return value.astimezone(UTC)
