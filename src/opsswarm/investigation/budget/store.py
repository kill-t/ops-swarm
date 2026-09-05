"""在有效父租约和同一行锁内预留、结算调用额度。"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.contracts import (
    CallBudgetPolicy,
    CallGrant,
    CallOperation,
    CallReservationRecord,
)
from opsswarm.investigation.budget.policy import (
    operation_kind,
    operation_role,
    remaining_time,
    require_capacity,
)
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.persistence.lease import (
    active_investigation_transaction,
    normalized_now,
)
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.tables import AgentTaskAttemptRow, AgentTaskRow


def utc_now() -> datetime:
    return datetime.now(UTC)


def _policy(run: InvestigationRunRow) -> CallBudgetPolicy:
    if run.workflow_version != "v2" or run.call_budget is None or run.started_at is None:
        raise TaskFactConflict("call_budget_not_enabled", "运行没有固定的调用预算")
    return CallBudgetPolicy.model_validate(run.call_budget)


class CallBudgetStore:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory = factory
        self._clock = clock

    async def check(self, run_id: UUID, *, lease_token: UUID) -> float:
        async with active_investigation_transaction(
            self._factory, run_id, lease_token, self._clock
        ) as (_, _, _, run, now):
            policy = _policy(run)
            assert run.started_at is not None
            return remaining_time(policy, run.started_at, now)

    async def reserve(
        self,
        run_id: UUID,
        *,
        lease_token: UUID,
        operation: CallOperation,
        task_id: UUID | None = None,
        task_attempt_id: UUID | None = None,
    ) -> CallGrant:
        async with active_investigation_transaction(
            self._factory, run_id, lease_token, self._clock
        ) as (session, parent, _, run, now):
            policy = _policy(run)
            assert run.started_at is not None
            remaining_time(policy, run.started_at, now)
            role = operation_role(operation)
            if role is None:
                if task_id is not None or task_attempt_id is not None:
                    raise TaskFactConflict("call_scope_invalid", "综合调用不能绑定专业尝试")
            else:
                task = await session.get(AgentTaskRow, task_id) if task_id is not None else None
                attempt = (
                    await session.get(AgentTaskAttemptRow, task_attempt_id)
                    if task_attempt_id is not None
                    else None
                )
                if (
                    task is None
                    or attempt is None
                    or task.investigation_run_id != run_id
                    or task.status != "running"
                    or task.assignment["role"] != role
                    or attempt.task_id != task.id
                    or attempt.investigation_run_id != run_id
                    or attempt.investigation_attempt_id != parent.id
                    or attempt.status != "running"
                ):
                    raise TaskFactConflict("call_scope_invalid", "调用不属于当前有效专业尝试")
            key = f"{parent.id}:{task_attempt_id or 'root'}:{operation.value}"
            records = list(
                await session.scalars(
                    select(CallReservationRow)
                    .where(
                        CallReservationRow.investigation_run_id == run_id,
                    )
                    .order_by(CallReservationRow.sequence)
                )
            )
            existing = next((r for r in records if r.idempotency_key == key), None)
            if existing is not None:
                return CallGrant(
                    CallReservationRecord.model_validate(existing, from_attributes=True),
                    False,
                    remaining_time(policy, run.started_at, normalized_now(self._clock)),
                )
            require_capacity(
                policy,
                operation,
                models=sum(r.kind == "model" for r in records),
                tools=sum(r.kind == "tool" for r in records),
                task_tools=sum(r.kind == "tool" and r.task_id == task_id for r in records),
            )
            row = CallReservationRow(
                id=uuid4(),
                investigation_run_id=run_id,
                investigation_attempt_id=parent.id,
                task_id=task_id,
                task_attempt_id=task_attempt_id,
                sequence=len(records) + 1,
                idempotency_key=key,
                operation=operation.value,
                kind=operation_kind(operation),
                status="reserved",
                error_code=None,
                reserved_at=now,
                finished_at=None,
            )
            session.add(row)
            await session.flush()
            remaining = remaining_time(policy, run.started_at, normalized_now(self._clock))
            return CallGrant(
                CallReservationRecord.model_validate(row, from_attributes=True), True, remaining
            )

    async def settle(
        self,
        run_id: UUID,
        reservation_id: UUID,
        *,
        lease_token: UUID,
        status: Literal["succeeded", "failed", "unknown"],
        error_code: Literal["operation_failed", "operation_cancelled", "run_deadline_exceeded"]
        | None = None,
    ) -> CallReservationRecord:
        if (status == "succeeded") != (error_code is None):
            raise ValueError("调用结算状态与错误码不一致")
        async with active_investigation_transaction(
            self._factory, run_id, lease_token, self._clock
        ) as (session, parent, _, _, now):
            row = await session.scalar(
                select(CallReservationRow).where(
                    CallReservationRow.id == reservation_id,
                    CallReservationRow.investigation_run_id == run_id,
                )
            )
            if row is None or row.investigation_attempt_id != parent.id:
                raise TaskFactConflict("call_reservation_not_owned", "预留不属于当前父尝试")
            if row.status != "reserved":
                if row.status != status or row.error_code != error_code:
                    raise TaskFactConflict("call_settlement_conflict", "调用已保存另一种结算结果")
            else:
                row.status = status
                row.error_code = error_code
                row.finished_at = now
            return CallReservationRecord.model_validate(row, from_attributes=True)
