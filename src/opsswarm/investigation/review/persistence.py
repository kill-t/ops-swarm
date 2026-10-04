"""独立审查写入、来源重建与跨父尝试恢复。"""

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
from opsswarm.investigation.persistence.lease import active_investigation_transaction
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review.contracts import (
    HypothesisReview,
    HypothesisReviewProposal,
    HypothesisReviewRecord,
    ReviewModelResult,
)
from opsswarm.investigation.review.policy import validate_review_evidence
from opsswarm.investigation.review.tables import InvestigationHypothesisReviewRow
from opsswarm.investigation.synthesis.persistence import validated_open_hypothesis
from opsswarm.investigation.synthesis.tables import InvestigationOpenHypothesisRow
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.tables import InvestigationRoundRow
from opsswarm.investigation.workflow_versions import (
    DYNAMIC_REVIEW_GRAPH_VERSIONS,
    DYNAMIC_THIRD_ROUND_REVIEW_GRAPH_VERSIONS,
)


def utc_now() -> datetime:
    return datetime.now(UTC)


async def validated_review_call(
    session: AsyncSession,
    run: InvestigationRunRow,
    call_id: UUID,
    *,
    expected_round_number: int | None = None,
) -> tuple[ModelInput, ReviewModelResult, ContextManifest]:
    """从当前业务材料重建一次成功独立审查调用。"""

    row = await session.scalar(
        select(CallReservationRow).where(
            CallReservationRow.id == call_id,
            CallReservationRow.investigation_run_id == run.id,
        )
    )
    if (
        row is None
        or run.workflow_version != "v3"
        or run.graph_version not in DYNAMIC_REVIEW_GRAPH_VERSIONS
        or row.operation != CallOperation.REVIEW.value
        or row.kind != "model"
        or row.status != "succeeded"
        or row.task_id is not None
        or row.task_attempt_id is not None
        or row.model_input is None
        or row.model_allocation is None
        or row.context_manifest is None
        or row.call_result is None
        or row.finished_at is None
        or row.invocation_index not in (0, 1, 2)
        or (expected_round_number is not None and row.invocation_index != expected_round_number - 1)
        or (
            row.invocation_index == 2
            and run.graph_version not in DYNAMIC_THIRD_ROUND_REVIEW_GRAPH_VERSIONS
        )
    ):
        raise TaskFactConflict("hypothesis_review_call_invalid", "独立审查缺少有效的成功模型调用")
    try:
        model_input = ModelInput.model_validate(row.model_input)
        result = ReviewModelResult.model_validate(row.call_result)
        manifest = ContextManifest.model_validate(row.context_manifest)
    except ValueError as error:
        raise TaskFactConflict(
            "hypothesis_review_call_invalid", "独立审查调用内容无法通过契约校验"
        ) from error
    await validate_context_reservation(
        session,
        run,
        CallOperation.REVIEW,
        None,
        model_input,
        manifest,
        manifest.prompt_version,
        invocation_index=row.invocation_index,
    )
    if (
        row.model_input != model_input.model_dump(mode="json")
        or row.context_manifest != manifest.model_dump(mode="json")
        or row.model_allocation != manifest.allocation.model_dump(mode="json")
        or row.call_result != result.model_dump(mode="json")
    ):
        raise TaskFactConflict(
            "hypothesis_review_call_invalid", "独立审查调用 JSON 与规范内容不一致"
        )
    return model_input, result, manifest


async def validated_hypothesis_review(
    session: AsyncSession, row: InvestigationHypothesisReviewRow
) -> HypothesisReviewRecord:
    """复核审查内容、开放假设和成功模型调用来源。"""

    run = await session.get(InvestigationRunRow, row.investigation_run_id)
    hypothesis_row = await session.get(InvestigationOpenHypothesisRow, row.open_hypothesis_id)
    if (
        run is None
        or hypothesis_row is None
        or hypothesis_row.investigation_run_id != row.investigation_run_id
        or hypothesis_row.round_id != row.round_id
    ):
        raise TaskFactConflict("hypothesis_review_source_invalid", "独立审查不属于当前开放假设")
    hypothesis = await validated_open_hypothesis(session, hypothesis_row)
    round_number = await session.scalar(
        select(InvestigationRoundRow.round_number).where(
            InvestigationRoundRow.id == row.round_id,
            InvestigationRoundRow.investigation_run_id == row.investigation_run_id,
        )
    )
    if round_number not in (1, 2, 3) or (
        round_number == 3 and run.graph_version not in DYNAMIC_THIRD_ROUND_REVIEW_GRAPH_VERSIONS
    ):
        raise TaskFactConflict("hypothesis_review_source_invalid", "独立审查轮次不受支持")
    successful_call_ids = tuple(
        await session.scalars(
            select(CallReservationRow.id)
            .where(
                CallReservationRow.investigation_run_id == run.id,
                CallReservationRow.operation == CallOperation.REVIEW.value,
                CallReservationRow.invocation_index == round_number - 1,
                CallReservationRow.status == "succeeded",
            )
            .order_by(CallReservationRow.sequence)
        )
    )
    if successful_call_ids != (row.source_call_id,):
        raise TaskFactConflict(
            "hypothesis_review_call_conflict", "当前轮次必须且只能存在一条成功独立审查调用"
        )
    _, called, manifest = await validated_review_call(
        session,
        run,
        row.source_call_id,
        expected_round_number=round_number,
    )
    source_call = await session.get(CallReservationRow, row.source_call_id)
    assert source_call is not None and source_call.finished_at is not None
    try:
        record = HypothesisReviewRecord.model_validate(row, from_attributes=True)
    except ValueError as error:
        raise TaskFactConflict(
            "hypothesis_review_integrity_violation", "独立审查内容无法通过校验"
        ) from error
    hypothesis_materials = [item for item in manifest.materials if item.kind == "open_hypothesis"]
    if (
        HypothesisReview.from_proposal(called.proposal) != record.review
        or len(hypothesis_materials) != 1
        or hypothesis_materials[0].material_id != hypothesis.id
        or hypothesis_materials[0].version != hypothesis.version
        or hypothesis_materials[0].content_sha256 != hypothesis.content_sha256
        or row.created_at < hypothesis.created_at
        or row.created_at < source_call.finished_at
    ):
        raise TaskFactConflict(
            "hypothesis_review_source_invalid", "独立审查与开放假设、调用或时间来源不一致"
        )
    return record


class HypothesisReviewStore:
    """只在有效父租约下提交或复用一份独立审查事实。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    async def get_existing(
        self, run_id: UUID, open_hypothesis_id: UUID, *, lease_token: UUID
    ) -> HypothesisReviewRecord | None:
        async with active_investigation_transaction(
            self._session_factory, run_id, lease_token, self._clock
        ) as (session, _, _, _, _):
            row = await session.scalar(
                select(InvestigationHypothesisReviewRow).where(
                    InvestigationHypothesisReviewRow.investigation_run_id == run_id,
                    InvestigationHypothesisReviewRow.open_hypothesis_id == open_hypothesis_id,
                )
            )
            return await validated_hypothesis_review(session, row) if row is not None else None

    async def save(
        self,
        run_id: UUID,
        open_hypothesis_id: UUID,
        *,
        lease_token: UUID,
        proposal: HypothesisReviewProposal,
        source_call_id: UUID,
    ) -> tuple[HypothesisReviewRecord, bool]:
        async with active_investigation_transaction(
            self._session_factory, run_id, lease_token, self._clock
        ) as (session, parent, _, run, now):
            hypothesis_row = await session.get(InvestigationOpenHypothesisRow, open_hypothesis_id)
            if hypothesis_row is None or hypothesis_row.investigation_run_id != run_id:
                raise TaskFactConflict(
                    "hypothesis_review_source_invalid", "独立审查缺少同运行开放假设"
                )
            hypothesis = await validated_open_hypothesis(session, hypothesis_row)
            round_number = await session.scalar(
                select(InvestigationRoundRow.round_number).where(
                    InvestigationRoundRow.id == hypothesis.round_id,
                    InvestigationRoundRow.investigation_run_id == run_id,
                )
            )
            if round_number not in (1, 2, 3) or (
                round_number == 3
                and run.graph_version not in DYNAMIC_THIRD_ROUND_REVIEW_GRAPH_VERSIONS
            ):
                raise TaskFactConflict("hypothesis_review_source_invalid", "独立审查轮次不受支持")
            _, called, manifest = await validated_review_call(
                session,
                run,
                source_call_id,
                expected_round_number=round_number,
            )
            if called.proposal != proposal:
                raise TaskFactConflict(
                    "hypothesis_review_call_mismatch", "独立审查与模型调用返回不一致"
                )
            hypothesis_materials = [
                item for item in manifest.materials if str(item.kind) == "open_hypothesis"
            ]
            if (
                len(hypothesis_materials) != 1
                or hypothesis_materials[0].material_id != open_hypothesis_id
                or hypothesis_materials[0].version != hypothesis.version
                or hypothesis_materials[0].content_sha256 != hypothesis.content_sha256
            ):
                raise TaskFactConflict(
                    "hypothesis_review_source_invalid", "独立审查上下文未绑定当前开放假设"
                )
            allowed = {
                evidence_id
                for item in manifest.materials
                if item.kind == "task_evidence"
                for evidence_id in item.evidence_ids
            }
            validate_review_evidence(proposal, hypothesis.hypothesis, allowed)
            review = HypothesisReview.from_proposal(proposal)
            existing = await session.scalar(
                select(InvestigationHypothesisReviewRow).where(
                    InvestigationHypothesisReviewRow.investigation_run_id == run_id,
                    InvestigationHypothesisReviewRow.open_hypothesis_id == open_hypothesis_id,
                )
            )
            if existing is not None:
                record = await validated_hypothesis_review(session, existing)
                if record.review != review or record.source_call_id != source_call_id:
                    raise TaskFactConflict(
                        "hypothesis_review_conflict", "开放假设已经保存另一份独立审查"
                    )
                return record, False
            row = InvestigationHypothesisReviewRow(
                id=uuid5(open_hypothesis_id, "hypothesis-review:primary"),
                investigation_run_id=run_id,
                round_id=hypothesis.round_id,
                open_hypothesis_id=open_hypothesis_id,
                investigation_attempt_id=parent.id,
                source_call_id=source_call_id,
                version=review.version,
                status=review.status,
                review=review.model_dump(mode="json"),
                content_sha256=content_hash(review),
                created_at=now,
            )
            session.add(row)
            await session.flush()
            return await validated_hypothesis_review(session, row), True
