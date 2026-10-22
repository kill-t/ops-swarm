"""重评命令、准备来源与成功模型调用的历史重建。"""

import json
from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_prepare.contracts import PreparationArtifact, PreparationSource
from opsswarm.investigation.human_prepare.queries import read_job as read_preparation
from opsswarm.investigation.human_prepare.queries import validated_source
from opsswarm.investigation.human_prepare.tables import HumanPreparationJobRow
from opsswarm.investigation.human_review.authorization import validate_admission
from opsswarm.investigation.human_review.contracts import (
    POLICY_VERSION,
    HumanReviewAuditRecord,
    HumanReviewError,
    HumanReviewJobView,
    HumanReviewResultView,
    ReassessCommand,
)
from opsswarm.investigation.human_review.tables import (
    HumanReviewAttemptRow,
    HumanReviewCommandRow,
    HumanReviewJobRow,
    HumanReviewResultRow,
)
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.review.contracts import ReviewModelResult
from opsswarm.investigation.review.policy import validate_review_evidence
from opsswarm.investigation.synthesis.contracts import OpenHypothesisRecord


def matches_command(job: HumanReviewJobRow, preparation_id: UUID, command: ReassessCommand) -> bool:
    return (
        job.preparation_job_id == preparation_id
        and job.preparation_sha256 == command.preparation_sha256
        and job.wait_version == command.expected_wait_version
        and job.idempotency_key == command.idempotency_key
        and job.command_sha256 == content_hash(command)
    )


async def validated_audit(
    session: AsyncSession, row: HumanReviewCommandRow
) -> HumanReviewAuditRecord:
    record = HumanReviewAuditRecord.model_validate(row, from_attributes=True)
    validate_admission(
        await session.get(OperatorAuthorizationRow, row.authorization_id),
        run_id=row.investigation_run_id,
        preparation_id=row.preparation_job_id,
        subject_id=row.subject_id,
        command=record.command,
        raw_body=row.raw_body,
        occurred_at=row.created_at,
    )
    if row.command != record.command.model_dump(mode="json") or row.command_sha256 != content_hash(
        record.command
    ):
        raise HumanReviewError("human_review_integrity_violation", "重评规范命令不一致")
    if row.outcome != "error":
        job = await session.get(HumanReviewJobRow, row.job_id)
        if (
            job is None
            or job.investigation_run_id != row.investigation_run_id
            or job.requested_by_id != row.subject_id
            or not matches_command(job, row.preparation_job_id, record.command)
            or row.created_at < job.created_at
            or (
                row.outcome == "applied"
                and (
                    job.authorization_id != row.authorization_id or job.created_at != row.created_at
                )
            )
        ):
            raise HumanReviewError("human_review_integrity_violation", "重评命令缺少精确任务来源")
    return record


async def preparation_source(
    session: AsyncSession,
    run: InvestigationRunRow,
    preparation_id: UUID,
    *,
    clock: Callable[[], datetime],
) -> tuple[PreparationSource, PreparationArtifact, str]:
    prepared = await session.get(HumanPreparationJobRow, preparation_id)
    if prepared is None or prepared.investigation_run_id != run.id:
        raise HumanReviewError("human_review_preparation_not_found", "准备任务不存在", 404)
    view = await read_preparation(session, prepared, run, clock=clock)
    if view.status != "succeeded" or view.artifact is None:
        raise HumanReviewError("human_review_preparation_required", "重评需要成功的完整准备工件")
    return await validated_source(session, prepared, run), view.artifact, view.availability


async def job_source(
    session: AsyncSession,
    job: HumanReviewJobRow,
    run: InvestigationRunRow,
    *,
    clock: Callable[[], datetime],
) -> tuple[PreparationSource, PreparationArtifact, str]:
    source, artifact, availability = await preparation_source(
        session, run, job.preparation_job_id, clock=clock
    )
    creator = await session.scalar(
        select(HumanReviewCommandRow).where(
            HumanReviewCommandRow.authorization_id == job.authorization_id
        )
    )
    if (
        job.policy_version != POLICY_VERSION
        or job.id != uuid5(job.preparation_job_id, "human-review:v1")
        or job.investigation_run_id != run.id
        or job.wait_version != source.wait.state_version
        or job.preparation_sha256 != content_hash(artifact)
        or job.created_at < artifact.diagnostic.checked_at
        or creator is None
        or creator.outcome != "applied"
        or creator.job_id != job.id
    ):
        raise HumanReviewError("human_review_integrity_violation", "重评任务来源不一致")
    audit = await validated_audit(session, creator)
    if (
        audit.command.handoff_id != source.handoff.id
        or audit.command.source_sha256 != source.handoff.source_sha256
    ):
        raise HumanReviewError("human_review_integrity_violation", "重评命令不匹配原交接")
    return source, artifact, availability


def validate_result(source: PreparationSource, result: ReviewModelResult) -> None:
    # 来源已经按原审查调用重建，只允许原假设和原完整证据中的引用。
    material = json.loads(next(m.content for m in source.prior_input.messages if m.role == "user"))
    hypothesis = OpenHypothesisRecord.model_validate(material["open_hypothesis"])
    validate_review_evidence(
        result.proposal,
        hypothesis.hypothesis,
        [item["evidence_id"] for item in material["evidence"]],
    )


async def validated_call(
    session: AsyncSession,
    job: HumanReviewJobRow,
    source: PreparationSource,
    artifact: PreparationArtifact,
    call_id: UUID,
) -> CallReservationRow:
    call = await session.get(CallReservationRow, call_id)
    owner = (
        await session.get(HumanReviewAttemptRow, call.human_review_attempt_id)
        if call and call.human_review_attempt_id
        else None
    )
    prepared = artifact.prepared_input
    if (
        call is None
        or owner is None
        or owner.job_id != job.id
        or owner.investigation_run_id != job.investigation_run_id
        or call.investigation_run_id != job.investigation_run_id
        or call.investigation_attempt_id != source.handoff.investigation_attempt_id
        or call.operation != "human_review"
        or call.kind != "model"
        or call.status != "succeeded"
        or call.task_id is not None
        or call.task_attempt_id is not None
        or call.invocation_index != 0
        or call.idempotency_key != f"human-review:{owner.id}"
        or prepared.model_input is None
        or prepared.allocation is None
        or call.model_input != prepared.model_input.model_dump(mode="json")
        or call.model_allocation != prepared.allocation.model_dump(mode="json")
        or call.context_manifest != prepared.manifest.model_dump(mode="json")
        or call.finished_at is None
        or not owner.started_at <= call.reserved_at <= call.finished_at < owner.lease_expires_at
        or call.call_result is None
    ):
        raise HumanReviewError("human_review_call_invalid", "重评缺少同任务成功调用")
    result = ReviewModelResult.model_validate(call.call_result)
    if call.call_result != result.model_dump(mode="json"):
        raise HumanReviewError("human_review_call_invalid", "重评模型结果表示不一致")
    validate_result(source, result)
    return call


async def read_job(
    session: AsyncSession,
    job: HumanReviewJobRow,
    run: InvestigationRunRow,
    *,
    clock: Callable[[], datetime],
) -> HumanReviewJobView:
    source, artifact, availability = await job_source(session, job, run, clock=clock)
    attempts = list(
        await session.scalars(
            select(HumanReviewAttemptRow)
            .where(HumanReviewAttemptRow.job_id == job.id)
            .order_by(HumanReviewAttemptRow.number)
        )
    )
    if len(attempts) != job.attempt_count or any(a.number != i + 1 for i, a in enumerate(attempts)):
        raise HumanReviewError("human_review_integrity_violation", "重评尝试序列不连续")
    row = await session.get(HumanReviewResultRow, job.id)
    result = None
    if row is not None:
        call = await validated_call(session, job, source, artifact, row.source_call_id)
        result = HumanReviewResultView.model_validate(row, from_attributes=True)
        if (
            job.status != "succeeded"
            or row.content != call.call_result
            or content_hash(result.content) != row.content_sha256
            or job.completed_at != row.created_at
            or not attempts
            or attempts[-1].id != row.attempt_id
            or attempts[-1].status != "succeeded"
            or attempts[-1].finished_at != row.created_at
        ):
            raise HumanReviewError("human_review_integrity_violation", "重评结果与成功闭环不一致")
    elif job.status == "succeeded":
        raise HumanReviewError("human_review_integrity_violation", "成功重评缺少结果")
    current = (
        "stale"
        if availability == "stale"
        else "failed"
        if job.status == "failed"
        else "current"
        if result
        else "pending"
    )
    return HumanReviewJobView.model_validate(
        {
            **{
                k: getattr(job, k)
                for k in (
                    "id",
                    "investigation_run_id",
                    "preparation_job_id",
                    "preparation_sha256",
                    "wait_version",
                    "policy_version",
                    "requested_by_id",
                    "authorization_id",
                    "status",
                    "created_at",
                    "completed_at",
                    "last_error_code",
                )
            },
            "attempts": attempts,
            "result": result,
            "availability": current,
        },
        from_attributes=True,
    )
