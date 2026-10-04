"""在有效父租约和同一行锁内预留、结算调用额度。"""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import defer

from opsswarm.investigation.budget.contracts import (
    CallBudgetPolicy,
    CallGrant,
    CallOperation,
    CallReservationRecord,
)
from opsswarm.investigation.budget.identity import reservation_key
from opsswarm.investigation.budget.model_contracts import (
    ModelAllocation,
    ModelBudgetPolicy,
    ReportedModelUsage,
)
from opsswarm.investigation.budget.model_policy import (
    allocate_input,
    require_model_capacity,
    summarize_model_usage,
)
from opsswarm.investigation.budget.policy import (
    CallBudgetExceeded,
    operation_kind,
    operation_role,
    remaining_time,
    require_capacity,
)
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.contracts import ContextManifest
from opsswarm.investigation.context.reservation import validate_context_reservation
from opsswarm.investigation.loop.contracts import AnyModelInput, CallResult, EvidenceToolRequest
from opsswarm.investigation.loop.ledger import validate_call_result, validate_tool_request
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
    if (
        run.workflow_version not in ("v2", "v3")
        or run.call_budget is None
        or run.started_at is None
    ):
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
        ) as (session, _, _, run, now):
            policy = _policy(run)
            assert run.started_at is not None
            if run.model_budget is not None:
                model_policy = ModelBudgetPolicy.model_validate(run.model_budget)
                records = await session.scalars(
                    select(CallReservationRow)
                    .options(defer(CallReservationRow.model_input))
                    .where(
                        CallReservationRow.investigation_run_id == run_id,
                        CallReservationRow.kind == "model",
                    )
                )
                items = [
                    (
                        ModelAllocation.model_validate(row.model_allocation),
                        ReportedModelUsage.model_validate(row.model_usage)
                        if row.model_usage
                        else None,
                    )
                    for row in records
                ]
                require_model_capacity(model_policy, summarize_model_usage(model_policy, items))
                if any(
                    usage is not None
                    and (
                        usage.input_tokens > allocation.estimated_input_tokens
                        or usage.output_tokens > allocation.output_token_limit
                    )
                    for allocation, usage in items
                ):
                    raise CallBudgetExceeded("model_usage_exceeded_allocation")
            return remaining_time(policy, run.started_at, now)

    async def reserve(
        self,
        run_id: UUID,
        *,
        lease_token: UUID,
        operation: CallOperation,
        task_id: UUID | None = None,
        task_attempt_id: UUID | None = None,
        model_input: AnyModelInput | None = None,
        prompt_version: str | None = None,
        context_manifest: ContextManifest | None = None,
        invocation_index: int = 0,
        tool_request: EvidenceToolRequest | None = None,
    ) -> CallGrant:
        if operation == CallOperation.COORDINATE and invocation_index != 0:
            raise TaskFactConflict(
                "coordination_invocation_invalid", "首轮协调调用的调用序号必须为零"
            )
        if operation == CallOperation.REVIEW and invocation_index not in (0, 1, 2):
            raise TaskFactConflict(
                "hypothesis_review_invocation_invalid", "独立审查调用的调用序号必须为零、一或二"
            )
        async with active_investigation_transaction(
            self._factory, run_id, lease_token, self._clock
        ) as (session, parent, _, run, now):
            key = reservation_key(parent.id, task_attempt_id, operation, invocation_index)
            policy = _policy(run)
            assert run.started_at is not None
            remaining_time(policy, run.started_at, now)
            model_policy = (
                ModelBudgetPolicy.model_validate(run.model_budget) if run.model_budget else None
            )
            allocation = None
            if model_policy is not None and operation_kind(operation) == "model":
                if model_input is None or prompt_version is None:
                    raise CallBudgetExceeded("model_input_required")
                allocation = allocate_input(
                    model_policy, model_input, prompt_version=prompt_version
                )
            elif model_input is not None or prompt_version is not None:
                raise TaskFactConflict("model_input_not_allowed", "该调用不接受模型输入")
            role = operation_role(operation)
            if role is None:
                if task_id is not None or task_attempt_id is not None:
                    raise TaskFactConflict("call_scope_invalid", "根级模型调用不能绑定专业尝试")
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
            await validate_context_reservation(
                session,
                run,
                operation,
                task_id,
                model_input,
                context_manifest,
                prompt_version,
                task_attempt_id,
                invocation_index,
            )
            await validate_tool_request(
                session, run, task_id, task_attempt_id, operation, invocation_index, tool_request
            )
            request_data = tool_request.model_dump(mode="json") if tool_request else None
            manifest_data = context_manifest.model_dump(mode="json") if context_manifest else None
            records = list(
                await session.scalars(
                    select(CallReservationRow)
                    .options(defer(CallReservationRow.model_input))
                    .where(
                        CallReservationRow.investigation_run_id == run_id,
                    )
                    .order_by(CallReservationRow.sequence)
                )
            )
            existing = next((r for r in records if r.idempotency_key == key), None)
            if existing is not None:
                if existing.tool_request != request_data:
                    raise TaskFactConflict("tool_input_replay_conflict", "同一读取不能改写参数")
                if existing.context_manifest != manifest_data:
                    raise TaskFactConflict("context_replay_conflict", "同一预留不能改写上下文清单")
                if existing.model_allocation != (
                    allocation.model_dump(mode="json") if allocation else None
                ):
                    raise TaskFactConflict(
                        "model_input_replay_conflict", "同一预留不能改用另一份模型输入"
                    )
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
            if model_policy is not None and allocation is not None:
                usage = summarize_model_usage(
                    model_policy,
                    [
                        (
                            ModelAllocation.model_validate(r.model_allocation),
                            ReportedModelUsage.model_validate(r.model_usage)
                            if r.model_usage
                            else None,
                        )
                        for r in records
                        if r.kind == "model"
                    ],
                )
                require_model_capacity(model_policy, usage, allocation)
            row = CallReservationRow(
                id=uuid4(),
                investigation_run_id=run_id,
                investigation_attempt_id=parent.id,
                task_id=task_id,
                task_attempt_id=task_attempt_id,
                sequence=len(records) + 1,
                invocation_index=invocation_index,
                idempotency_key=key,
                operation=operation.value,
                kind=operation_kind(operation),
                status="reserved",
                error_code=None,
                reserved_at=now,
                finished_at=None,
                model_input=model_input.model_dump(mode="json") if model_input else None,
                model_allocation=allocation.model_dump(mode="json") if allocation else None,
                model_usage=None,
                context_manifest=manifest_data,
                tool_request=request_data,
                call_result=None,
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
        model_usage: ReportedModelUsage | None = None,
        call_result: CallResult | None = None,
    ) -> CallReservationRecord:
        if (status == "succeeded") != (error_code is None):
            raise ValueError("调用结算状态与错误码不一致")
        async with active_investigation_transaction(
            self._factory, run_id, lease_token, self._clock
        ) as (session, parent, _, run, now):
            row = await session.scalar(
                select(CallReservationRow).where(
                    CallReservationRow.id == reservation_id,
                    CallReservationRow.investigation_run_id == run_id,
                )
            )
            if row is None or row.investigation_attempt_id != parent.id:
                raise TaskFactConflict("call_reservation_not_owned", "预留不属于当前父尝试")
            if model_usage is not None and row.model_allocation is None:
                raise TaskFactConflict("model_usage_not_allowed", "该调用没有模型输入预留")
            usage_data = model_usage.model_dump(mode="json") if model_usage is not None else None
            result_data = call_result.model_dump(mode="json") if call_result is not None else None
            if row.status != "reserved":
                if (
                    row.status != status
                    or row.error_code != error_code
                    or row.model_usage != usage_data
                    or row.call_result != result_data
                ):
                    raise TaskFactConflict("call_settlement_conflict", "调用已保存另一种结算结果")
            else:
                await validate_call_result(session, run, row, status, call_result)
                row.status = status
                row.error_code = error_code
                row.finished_at = now
                row.model_usage = usage_data
                row.call_result = result_data
            return CallReservationRecord.model_validate(row, from_attributes=True)
