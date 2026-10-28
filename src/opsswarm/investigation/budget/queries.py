"""从有界预留账本读取预算状态，不推断供应商用量。"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
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
from opsswarm.investigation.coordination.contracts import CoordinationModelInput
from opsswarm.investigation.errors import InvestigationRunNotFound
from opsswarm.investigation.loop.contracts import (
    MODEL_INPUT_ADAPTER,
    AnyModelInput,
    CallInteraction,
)
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.tasks.errors import TaskFactConflict

if TYPE_CHECKING:
    from opsswarm.investigation.candidate.contracts import CandidateManifest
    from opsswarm.investigation.human_prepare.contracts import PreparationManifest
    from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowManifest
    from opsswarm.investigation.reinvestigation_execution.contracts import ExecutionManifest
    from opsswarm.investigation.reinvestigation_synthesis.contracts import SynthesisManifest


class CallBudgetQueries:
    def __init__(self, factory: async_sessionmaker[AsyncSession]) -> None:
        self._factory = factory

    async def requires_trusted_read(self, run_id: UUID, call_id: UUID) -> bool:
        async with self._factory() as session:
            operation = await session.scalar(
                select(CallReservationRow.operation).where(
                    CallReservationRow.investigation_run_id == run_id,
                    CallReservationRow.id == call_id,
                )
            )
            return operation in (
                "coordinate_reinvestigation",
                "synthesize_reinvestigation",
                "collect_reinvestigation",
                "analyze_reinvestigation",
            )

    async def get_snapshot(self, run_id: UUID) -> CallBudgetSnapshot:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, run_id)
            if run is None:
                raise InvestigationRunNotFound(run_id)
            return await read_budget_snapshot(session, run)

    async def get_model_input(self, run_id: UUID, call_id: UUID) -> AnyModelInput | None:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, run_id)
            if run is None:
                raise InvestigationRunNotFound(run_id)
            row = await session.scalar(
                select(CallReservationRow).where(
                    CallReservationRow.investigation_run_id == run_id,
                    CallReservationRow.id == call_id,
                )
            )
            if row is None:
                raise TaskFactConflict("model_input_not_found", "该运行不存在指定调用")
            await _validate_experience_context(session, run, row)
            return (
                MODEL_INPUT_ADAPTER.validate_python(row.model_input)
                if row.model_input is not None
                else None
            )

    async def get_interaction(self, run_id: UUID, call_id: UUID) -> CallInteraction:
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, run_id)
            if run is None:
                raise InvestigationRunNotFound(run_id)
            row = await session.scalar(
                select(CallReservationRow).where(
                    CallReservationRow.investigation_run_id == run_id,
                    CallReservationRow.id == call_id,
                )
            )
            if row is None:
                raise TaskFactConflict("call_interaction_not_found", "该运行不存在指定调用")
            if row.operation == "synthesize_reinvestigation":
                await _synthesis_manifest(session, run, row)
            if row.operation in ("collect_reinvestigation", "analyze_reinvestigation"):
                await _execution_manifest(session, run, row)
            if row.operation == "coordinate_reinvestigation":
                await _reflow_manifest(session, run, row)
            if row.operation == "evaluate_candidate":
                await _candidate_manifest(session, run, row)
            if row.operation == "human_review":
                await _human_review_manifest(session, run, row)
            return CallInteraction.model_validate(
                {
                    "investigation_run_id": run_id,
                    "call_id": call_id,
                    "tool_request": row.tool_request,
                    "result": row.call_result,
                }
            )

    async def get_context_manifest(
        self, run_id: UUID, call_id: UUID
    ) -> (
        ContextManifest
        | PreparationManifest
        | CandidateManifest
        | ReflowManifest
        | ExecutionManifest
        | SynthesisManifest
        | None
    ):
        async with self._factory() as session:
            run = await session.get(InvestigationRunRow, run_id)
            if run is None:
                raise InvestigationRunNotFound(run_id)
            row = await session.scalar(
                select(CallReservationRow).where(
                    CallReservationRow.investigation_run_id == run_id,
                    CallReservationRow.id == call_id,
                )
            )
            if row is None:
                raise TaskFactConflict("context_manifest_not_found", "该运行不存在指定调用")
            await _validate_experience_context(session, run, row)
            if row.context_manifest is None:
                return None
            if row.operation in ("collect_reinvestigation", "analyze_reinvestigation"):
                return await _execution_manifest(session, run, row)
            if row.operation == "synthesize_reinvestigation":
                return await _synthesis_manifest(session, run, row)
            if row.operation == "coordinate_reinvestigation":
                return await _reflow_manifest(session, run, row)
            if row.operation == "evaluate_candidate":
                return await _candidate_manifest(session, run, row)
            if row.operation == "human_review":
                return await _human_review_manifest(session, run, row)
            manifest = ContextManifest.model_validate(row.context_manifest)
            if (
                manifest.investigation_run_id != run_id
                or manifest.task_id != row.task_id
                or manifest.allocation.model_dump(mode="json") != row.model_allocation
            ):
                raise TaskFactConflict("context_manifest_corrupt", "上下文清单与调用记录不一致")
            return manifest


async def _validate_experience_context(
    session: AsyncSession, run: InvestigationRunRow, row: CallReservationRow
) -> None:
    if row.operation == "synthesize_reinvestigation":
        await _synthesis_manifest(session, run, row)
        return
    if row.operation in ("collect_reinvestigation", "analyze_reinvestigation"):
        await _execution_manifest(session, run, row)
        return
    if row.operation == "coordinate_reinvestigation":
        await _reflow_manifest(session, run, row)
        return
    if row.operation == "evaluate_candidate":
        await _candidate_manifest(session, run, row)
        return
    if row.operation == "human_review":
        await _human_review_manifest(session, run, row)
        return
    if row.operation != "coordinate" or run.coordination_experience_policy_version is None:
        return
    # 历史调用查询也复核冻结工件，但不要求调用成功，失败预留同样是审计事实。
    from opsswarm.investigation.coordination.context import validate_coordination_context

    try:
        model_input = CoordinationModelInput.model_validate(row.model_input)
        manifest = ContextManifest.model_validate(row.context_manifest)
    except ValueError as error:
        raise TaskFactConflict(
            "experience_context_call_conflict", "协调经验调用审计不完整"
        ) from error
    if manifest.allocation.model_dump(mode="json") != row.model_allocation:
        raise TaskFactConflict("experience_context_call_conflict", "协调经验预算分配不一致")
    await validate_coordination_context(session, run, model_input, manifest)


async def read_budget_snapshot(
    session: AsyncSession, run: InvestigationRunRow
) -> CallBudgetSnapshot:
    """在调用方事务中读取原账本，准备诊断不创建第二套额度。"""
    policy = (
        CallBudgetPolicy.model_validate(run.call_budget) if run.call_budget is not None else None
    )
    model_policy = ModelBudgetPolicy.model_validate(run.model_budget) if run.model_budget else None
    rows = list(
        await session.scalars(
            select(CallReservationRow)
            .options(defer(CallReservationRow.model_input))
            .where(
                CallReservationRow.investigation_run_id == run.id,
            )
            .order_by(CallReservationRow.sequence)
        )
    )
    return CallBudgetSnapshot(
        investigation_run_id=run.id,
        policy=policy,
        started_at=run.started_at,
        deadline=run.started_at + timedelta(seconds=policy.max_duration_seconds)
        if policy and run.started_at
        else None,
        reserved_model_calls=sum(r.kind == "model" for r in rows) if policy else None,
        reserved_tool_calls=sum(r.kind == "tool" for r in rows) if policy else None,
        calls=tuple(CallReservationRecord.model_validate(r, from_attributes=True) for r in rows),
        model_budget=model_policy,
        model_usage=summarize_model_usage(
            model_policy,
            [
                (
                    ModelAllocation.model_validate(r.model_allocation),
                    ReportedModelUsage.model_validate(r.model_usage) if r.model_usage else None,
                )
                for r in rows
                if r.kind == "model"
            ],
        )
        if model_policy
        else None,
    )


async def _human_review_manifest(
    session: AsyncSession, run: InvestigationRunRow, row: CallReservationRow
) -> PreparationManifest:
    from opsswarm.investigation.human_review.queries import job_source
    from opsswarm.investigation.human_review.store import utc_now
    from opsswarm.investigation.human_review.tables import HumanReviewAttemptRow, HumanReviewJobRow

    attempt = (
        await session.get(HumanReviewAttemptRow, row.human_review_attempt_id)
        if row.human_review_attempt_id
        else None
    )
    job = await session.get(HumanReviewJobRow, attempt.job_id) if attempt else None
    if job is None:
        raise TaskFactConflict("human_review_context_invalid", "重评调用缺少真实阶段身份")
    source, artifact, _ = await job_source(session, job, run, clock=utc_now)
    prepared = artifact.prepared_input
    if (
        row.investigation_run_id != run.id
        or row.investigation_attempt_id != source.handoff.investigation_attempt_id
        or prepared.model_input is None
        or prepared.allocation is None
        or row.model_input != prepared.model_input.model_dump(mode="json")
        or row.context_manifest != prepared.manifest.model_dump(mode="json")
        or row.model_allocation != prepared.allocation.model_dump(mode="json")
    ):
        raise TaskFactConflict("human_review_context_invalid", "重评调用不匹配完整准备材料")
    return prepared.manifest


async def _candidate_manifest(
    session: AsyncSession, run: InvestigationRunRow, row: CallReservationRow
) -> CandidateManifest:
    from opsswarm.investigation.candidate.queries import job_source
    from opsswarm.investigation.candidate.store import utc_now
    from opsswarm.investigation.candidate.tables import CandidateAttemptRow, CandidateJobRow

    attempt = (
        await session.get(CandidateAttemptRow, row.candidate_attempt_id)
        if row.candidate_attempt_id
        else None
    )
    job = await session.get(CandidateJobRow, attempt.job_id) if attempt else None
    if job is None:
        raise TaskFactConflict("candidate_context_invalid", "候选调用缺少真实阶段身份")
    source, packet, _ = await job_source(session, job, run, clock=utc_now)
    if (
        row.investigation_run_id != run.id
        or row.investigation_attempt_id != source.handoff.investigation_attempt_id
        or row.model_input != packet.model_input.model_dump(mode="json")
        or row.model_allocation != packet.allocation.model_dump(mode="json")
        or row.context_manifest != packet.manifest.model_dump(mode="json")
    ):
        raise TaskFactConflict("candidate_context_invalid", "候选调用不匹配完整冻结输入")
    return packet.manifest


async def _reflow_manifest(
    session: AsyncSession, run: InvestigationRunRow, row: CallReservationRow
) -> ReflowManifest:
    from opsswarm.execution.release.observer import utc_now
    from opsswarm.investigation.reinvestigation_coordination.queries import (
        frozen_packet,
        validate_call_binding,
    )
    from opsswarm.investigation.reinvestigation_coordination.tables import (
        ReflowAttemptRow,
        ReflowJobRow,
    )

    owner = (
        await session.get(ReflowAttemptRow, row.reinvestigation_attempt_id)
        if row.reinvestigation_attempt_id
        else None
    )
    job = await session.get(ReflowJobRow, owner.job_id) if owner else None
    if job is None:
        raise TaskFactConflict("reflow_context_invalid", "回流调用缺少真实阶段归属")
    source, packet = await frozen_packet(session, run, job, clock=utc_now)
    await validate_call_binding(session, job, row, source=source, packet=packet)
    return packet.manifest


async def _execution_manifest(
    session: AsyncSession, run: InvestigationRunRow, row: CallReservationRow
) -> ExecutionManifest | None:
    from opsswarm.execution.release.observer import utc_now
    from opsswarm.investigation.reinvestigation_execution.source import load_source, validate_work
    from opsswarm.investigation.reinvestigation_execution.tables import ExecutionWorkRow
    from opsswarm.investigation.reinvestigation_execution.validation import (
        validate_call,
        validated_packet,
    )

    work = await session.get(ExecutionWorkRow, row.reflow_task_id) if row.reflow_task_id else None
    if work is None or work.investigation_run_id != run.id:
        raise TaskFactConflict("execution_context_invalid", "专业调用缺少真实独立任务")
    source = await load_source(session, run, work.task_id, clock=utc_now)
    validate_work(work, source)
    await validate_call(session, work, row, source, row.operation)
    if row.operation == "collect_reinvestigation":
        return None
    packet = await validated_packet(session, work, source)
    if packet is None:
        raise TaskFactConflict("execution_context_invalid", "专业模型调用缺少冻结输入")
    return packet.manifest


async def _synthesis_manifest(
    session: AsyncSession, run: InvestigationRunRow, row: CallReservationRow
) -> SynthesisManifest:
    from opsswarm.execution.release.observer import utc_now
    from opsswarm.investigation.reinvestigation_synthesis.queries import (
        frozen_packet,
        validate_call_binding,
    )
    from opsswarm.investigation.reinvestigation_synthesis.tables import SynthesisJobRow

    job = await session.get(SynthesisJobRow, run.id)
    if job is None:
        raise TaskFactConflict("synthesis_context_invalid", "综合调用缺少独立阶段")
    source, packet = await frozen_packet(session, run, job, clock=utc_now)
    await validate_call_binding(session, job, row, source=source, packet=packet)
    return packet.manifest
