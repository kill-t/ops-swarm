"""从有界预留账本读取预算状态，不推断供应商用量。"""

from datetime import timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import defer

from opsswarm.investigation.budget.contracts import (
    CallBudgetPolicy,
    CallBudgetSnapshot,
    CallReservationRecord,
)
from opsswarm.investigation.budget.model_contracts import (
    ModelAllocation,
    ModelBudgetPolicy,
    ReportedModelUsage,
)
from opsswarm.investigation.budget.model_policy import summarize_model_usage
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.contracts import ContextManifest
from opsswarm.investigation.errors import InvestigationRunNotFound
from opsswarm.investigation.loop.contracts import (
    MODEL_INPUT_ADAPTER,
    AnyModelInput,
    CallInteraction,
)
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict


class CallBudgetQueries:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def get_snapshot(self, run_id: UUID) -> CallBudgetSnapshot:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, run_id)
            if run is None:
                raise InvestigationRunNotFound(run_id)
            policy = (
                CallBudgetPolicy.model_validate(run.call_budget)
                if run.call_budget is not None
                else None
            )
            model_policy = (
                ModelBudgetPolicy.model_validate(run.model_budget) if run.model_budget else None
            )
            rows = list(
                await session.scalars(
                    select(CallReservationRow)
                    .options(defer(CallReservationRow.model_input))
                    .where(
                        CallReservationRow.investigation_run_id == run_id,
                    )
                    .order_by(CallReservationRow.sequence)
                )
            )
            return CallBudgetSnapshot(
                investigation_run_id=run_id,
                policy=policy,
                started_at=run.started_at,
                deadline=run.started_at + timedelta(seconds=policy.max_duration_seconds)
                if policy and run.started_at
                else None,
                reserved_model_calls=sum(r.kind == "model" for r in rows) if policy else None,
                reserved_tool_calls=sum(r.kind == "tool" for r in rows) if policy else None,
                calls=tuple(
                    CallReservationRecord.model_validate(r, from_attributes=True) for r in rows
                ),
                model_budget=model_policy,
                model_usage=summarize_model_usage(
                    model_policy,
                    [
                        (
                            ModelAllocation.model_validate(r.model_allocation),
                            ReportedModelUsage.model_validate(r.model_usage)
                            if r.model_usage
                            else None,
                        )
                        for r in rows
                        if r.kind == "model"
                    ],
                )
                if model_policy
                else None,
            )

    async def get_model_input(self, run_id: UUID, call_id: UUID) -> AnyModelInput | None:
        async with self._factory() as session:
            if await session.get(InvestigationRunRow, run_id) is None:
                raise InvestigationRunNotFound(run_id)
            row = await session.scalar(
                select(CallReservationRow).where(
                    CallReservationRow.investigation_run_id == run_id,
                    CallReservationRow.id == call_id,
                )
            )
            if row is None:
                raise TaskFactConflict("model_input_not_found", "该运行不存在指定调用")
            return (
                MODEL_INPUT_ADAPTER.validate_python(row.model_input)
                if row.model_input is not None
                else None
            )

    async def get_interaction(self, run_id: UUID, call_id: UUID) -> CallInteraction:
        async with self._factory() as session:
            if await session.get(InvestigationRunRow, run_id) is None:
                raise InvestigationRunNotFound(run_id)
            row = await session.scalar(
                select(CallReservationRow).where(
                    CallReservationRow.investigation_run_id == run_id,
                    CallReservationRow.id == call_id,
                )
            )
            if row is None:
                raise TaskFactConflict("call_interaction_not_found", "该运行不存在指定调用")
            return CallInteraction.model_validate(
                {
                    "investigation_run_id": run_id,
                    "call_id": call_id,
                    "tool_request": row.tool_request,
                    "result": row.call_result,
                }
            )

    async def get_context_manifest(self, run_id: UUID, call_id: UUID) -> ContextManifest | None:
        async with self._factory() as session:
            if await session.get(InvestigationRunRow, run_id) is None:
                raise InvestigationRunNotFound(run_id)
            row = await session.scalar(
                select(CallReservationRow)
                .options(defer(CallReservationRow.model_input))
                .where(
                    CallReservationRow.investigation_run_id == run_id,
                    CallReservationRow.id == call_id,
                )
            )
            if row is None:
                raise TaskFactConflict("context_manifest_not_found", "该运行不存在指定调用")
            if row.context_manifest is None:
                return None
            manifest = ContextManifest.model_validate(row.context_manifest)
            if (
                manifest.investigation_run_id != run_id
                or manifest.task_id != row.task_id
                or manifest.allocation.model_dump(mode="json") != row.model_allocation
            ):
                raise TaskFactConflict("context_manifest_corrupt", "上下文清单与调用记录不一致")
            return manifest
