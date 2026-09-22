"""开放假设写入、来源重建与跨父尝试恢复。"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.contracts import CallOperation
from opsswarm.investigation.budget.model_contracts import ModelInput
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.contracts import ContextManifest
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.context.reservation import validate_context_reservation
from opsswarm.investigation.coordination.persistence import validated_plan
from opsswarm.investigation.coordination.tables import InvestigationCoordinationPlanRow
from opsswarm.investigation.persistence.lease import active_investigation_transaction
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.synthesis.contracts import (
    OpenHypothesis,
    OpenHypothesisProposal,
    OpenHypothesisRecord,
    SynthesisModelResult,
)
from opsswarm.investigation.synthesis.policy import validate_proposal_evidence
from opsswarm.investigation.synthesis.tables import InvestigationOpenHypothesisRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.workflow_versions import DYNAMIC_SYNTHESIS_GRAPH_VERSIONS


def utc_now() -> datetime:
    return datetime.now(UTC)


async def validated_synthesis_call(
    session: AsyncSession,
    run: InvestigationRunRow,
    call_id: UUID,
) -> tuple[ModelInput, SynthesisModelResult, ContextManifest]:
    """从当前业务材料重建一次成功动态综合调用。"""

    row = await session.scalar(
        select(CallReservationRow).where(
            CallReservationRow.id == call_id,
            CallReservationRow.investigation_run_id == run.id,
        )
    )
    if (
        row is None
        or run.workflow_version != "v3"
        or run.graph_version not in DYNAMIC_SYNTHESIS_GRAPH_VERSIONS
        or row.operation != CallOperation.SYNTHESIZE.value
        or row.kind != "model"
        or row.status != "succeeded"
        or row.task_id is not None
        or row.task_attempt_id is not None
        or row.model_input is None
        or row.model_allocation is None
        or row.context_manifest is None
        or row.call_result is None
        or row.finished_at is None
    ):
        raise TaskFactConflict("dynamic_synthesis_call_invalid", "开放假设缺少有效的成功综合调用")
    try:
        model_input = ModelInput.model_validate(row.model_input)
        result = SynthesisModelResult.model_validate(row.call_result)
        manifest = ContextManifest.model_validate(row.context_manifest)
    except ValueError as error:
        raise TaskFactConflict(
            "dynamic_synthesis_call_invalid", "综合调用内容无法通过契约校验"
        ) from error
    await validate_context_reservation(
        session,
        run,
        CallOperation.SYNTHESIZE,
        None,
        model_input,
        manifest,
        manifest.prompt_version,
    )
    if (
        row.model_input != model_input.model_dump(mode="json")
        or row.context_manifest != manifest.model_dump(mode="json")
        or row.model_allocation != manifest.allocation.model_dump(mode="json")
        or row.call_result != result.model_dump(mode="json")
    ):
        raise TaskFactConflict("dynamic_synthesis_call_invalid", "综合调用 JSON 与规范内容不一致")
    return model_input, result, manifest


async def validated_open_hypothesis(
    session: AsyncSession, row: InvestigationOpenHypothesisRow
) -> OpenHypothesisRecord:
    """复核假设内容、首轮计划与成功模型来源。"""

    run = await session.get(InvestigationRunRow, row.investigation_run_id)
    plan_row = await session.get(InvestigationCoordinationPlanRow, row.round_id)
    if (
        run is None
        or run.workflow_version != "v3"
        or run.graph_version not in DYNAMIC_SYNTHESIS_GRAPH_VERSIONS
        or plan_row is None
        or plan_row.investigation_run_id != row.investigation_run_id
    ):
        raise TaskFactConflict("open_hypothesis_source_invalid", "开放假设不属于当前动态首轮计划")
    plan = await validated_plan(session, plan_row)
    if plan.plan.assignment.round_number != 1:
        raise TaskFactConflict("open_hypothesis_source_invalid", "当前开放假设只支持首轮协调计划")
    _, called, _ = await validated_synthesis_call(session, run, row.source_call_id)
    source_call = await session.get(CallReservationRow, row.source_call_id)
    assert source_call is not None and source_call.finished_at is not None
    try:
        record = OpenHypothesisRecord.model_validate(row, from_attributes=True)
    except ValueError as error:
        raise TaskFactConflict(
            "open_hypothesis_integrity_violation", "开放假设内容无法通过校验"
        ) from error
    if (
        OpenHypothesis.from_proposal(called.proposal) != record.hypothesis
        or row.created_at < plan.created_at
        or row.created_at < source_call.finished_at
    ):
        raise TaskFactConflict(
            "open_hypothesis_source_invalid", "开放假设与计划、调用或时间来源不一致"
        )
    return record


class OpenHypothesisStore:
    """只在有效父租约下提交或复用首轮开放假设。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def get_existing(
        self, run_id: UUID, round_id: UUID, *, lease_token: UUID
    ) -> OpenHypothesisRecord | None:
        async with active_investigation_transaction(
            self._session_factory, run_id, lease_token, self._clock
        ) as (session, _, _, _, _):
            row = await session.scalar(
                select(InvestigationOpenHypothesisRow).where(
                    InvestigationOpenHypothesisRow.investigation_run_id == run_id,
                    InvestigationOpenHypothesisRow.round_id == round_id,
                )
            )
            return await validated_open_hypothesis(session, row) if row is not None else None

    async def save(
        self,
        run_id: UUID,
        round_id: UUID,
        *,
        lease_token: UUID,
        proposal: OpenHypothesisProposal,
        source_call_id: UUID,
    ) -> tuple[OpenHypothesisRecord, bool]:
        async with active_investigation_transaction(
            self._session_factory, run_id, lease_token, self._clock
        ) as (session, parent, _, run, now):
            _, called, manifest = await validated_synthesis_call(session, run, source_call_id)
            if called.proposal != proposal:
                raise TaskFactConflict(
                    "open_hypothesis_call_mismatch", "开放假设与综合调用返回不一致"
                )
            plan_row = await session.get(InvestigationCoordinationPlanRow, round_id)
            if plan_row is None or plan_row.investigation_run_id != run_id:
                raise TaskFactConflict(
                    "open_hypothesis_source_invalid", "开放假设缺少同运行首轮计划"
                )
            plan = await validated_plan(session, plan_row)
            task_ids = {
                item.material_id for item in manifest.materials if item.kind == "task_evidence"
            }
            if (
                plan.plan.assignment.round_number != 1
                or task_ids
                != {
                    item.material_id
                    for item in manifest.materials
                    if item.kind == "task_working_summary"
                }
                or len(task_ids) != len(plan.plan.assignment.tasks)
            ):
                raise TaskFactConflict(
                    "open_hypothesis_source_invalid", "综合材料与首轮计划任务范围不一致"
                )
            allowed = {
                evidence_id
                for item in manifest.materials
                if item.kind == "task_evidence"
                for evidence_id in item.evidence_ids
            }
            validate_proposal_evidence(proposal, allowed)
            hypothesis = OpenHypothesis.from_proposal(proposal)
            existing = await session.scalar(
                select(InvestigationOpenHypothesisRow).where(
                    InvestigationOpenHypothesisRow.investigation_run_id == run_id,
                    InvestigationOpenHypothesisRow.round_id == round_id,
                )
            )
            if existing is not None:
                record = await validated_open_hypothesis(session, existing)
                if record.hypothesis != hypothesis or record.source_call_id != source_call_id:
                    raise TaskFactConflict("open_hypothesis_conflict", "首轮已经保存另一份开放假设")
                return record, False
            row = InvestigationOpenHypothesisRow(
                id=uuid5(round_id, "open-hypothesis:primary"),
                investigation_run_id=run_id,
                round_id=round_id,
                investigation_attempt_id=parent.id,
                source_call_id=source_call_id,
                version=hypothesis.version,
                status=hypothesis.status,
                hypothesis=hypothesis.model_dump(mode="json"),
                content_sha256=content_hash(hypothesis),
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return await validated_open_hypothesis(session, row), True
