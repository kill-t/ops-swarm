"""候选命令、冻结来源、调用归属与结果的完整历史重建。"""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.candidate.authorization import validate_admission
from opsswarm.investigation.candidate.context import build_packet
from opsswarm.investigation.candidate.contracts import (
    POLICY_VERSION,
    CandidateAuditRecord,
    CandidateError,
    CandidateJobView,
    CandidatePacket,
    CandidateResultView,
    CandidateSource,
    EvaluateCandidateCommand,
)
from opsswarm.investigation.candidate.evaluation import evaluate_candidate, validate_proposal
from opsswarm.investigation.candidate.output import CandidateModelResult
from opsswarm.investigation.candidate.source import load_source
from opsswarm.investigation.candidate.tables import (
    CandidateAttemptRow,
    CandidateCommandRow,
    CandidateJobRow,
    CandidateResultRow,
)
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.tables import InvestigationRunRow


def matches_command(job: CandidateJobRow, command: EvaluateCandidateCommand) -> bool:
    return (
        job.source_kind == command.source_kind
        and job.source_id == command.source_id
        and job.source_sha256 == command.source_sha256
        and job.wait_version == command.expected_wait_version
        and job.idempotency_key == command.idempotency_key
        and job.command_sha256 == content_hash(command)
    )


async def validated_audit(session: AsyncSession, row: CandidateCommandRow) -> CandidateAuditRecord:
    record = CandidateAuditRecord.model_validate(row, from_attributes=True)
    validate_admission(
        await session.get(OperatorAuthorizationRow, row.authorization_id),
        run_id=row.investigation_run_id,
        subject_id=row.subject_id,
        command=record.command,
        raw_body=row.raw_body,
        occurred_at=row.created_at,
    )
    if row.command != record.command.model_dump(mode="json") or row.command_sha256 != content_hash(
        record.command
    ):
        raise CandidateError("candidate_integrity_violation", "候选规范命令不一致")
    if row.outcome != "error":
        job = await session.get(CandidateJobRow, row.job_id)
        if (
            job is None
            or job.investigation_run_id != row.investigation_run_id
            or job.requested_by_id != row.subject_id
            or not matches_command(job, record.command)
            or row.created_at < job.created_at
            or row.outcome == "applied"
            and (job.authorization_id != row.authorization_id or job.created_at != row.created_at)
        ):
            raise CandidateError("candidate_integrity_violation", "候选审计缺少精确任务来源")
    return record


async def job_source(
    session: AsyncSession,
    job: CandidateJobRow,
    run: InvestigationRunRow,
    *,
    clock: Callable[[], datetime],
) -> tuple[CandidateSource, CandidatePacket, bool]:
    try:
        source = CandidateSource.model_validate(job.source)
        packet = CandidatePacket.model_validate(job.packet)
        creator = await session.scalar(
            select(CandidateCommandRow).where(
                CandidateCommandRow.authorization_id == job.authorization_id
            )
        )
        rebuilt, current = await load_source(
            session, run, source.source_kind, source.source_id, clock=clock
        )
        if (
            job.policy_version != POLICY_VERSION
            or job.investigation_run_id != run.id
            or job.id != uuid5(run.id, f"candidate:{job.source_kind}:{job.source_id}:v1")
            or source != rebuilt
            or source.model_dump(mode="json") != job.source
            or packet != build_packet(source)
            or packet.model_dump(mode="json") != job.packet
            or job.source_kind != source.source_kind
            or job.source_id != source.source_id
            or job.source_sha256 != source.source_sha256
            or job.source_content_sha256 != content_hash(source)
            or job.packet_sha256 != content_hash(packet)
            or job.wait_version != (source.human_route.wait_version if source.human_route else None)
            or creator is None
            or creator.outcome != "applied"
            or creator.job_id != job.id
            or job.created_at
            < (source.human_route.created_at if source.human_route else source.handoff.created_at)
        ):
            raise CandidateError("candidate_integrity_violation", "候选冻结来源或完整输入发生漂移")
        await validated_audit(session, creator)
        return source, packet, current
    except CandidateError:
        raise
    except ValueError as error:
        raise CandidateError("candidate_integrity_violation", "候选历史材料无法规范解码") from error


async def validated_call(
    session: AsyncSession,
    job: CandidateJobRow,
    source: CandidateSource,
    packet: CandidatePacket,
    call_id: UUID,
) -> CallReservationRow:
    try:
        call = await session.get(CallReservationRow, call_id)
        owner = (
            await session.get(CandidateAttemptRow, call.candidate_attempt_id)
            if call and call.candidate_attempt_id
            else None
        )
        if (
            call is None
            or owner is None
            or owner.job_id != job.id
            or owner.investigation_run_id != job.investigation_run_id
            or call.investigation_run_id != job.investigation_run_id
            or call.investigation_attempt_id != source.handoff.investigation_attempt_id
            or call.operation != "evaluate_candidate"
            or call.kind != "model"
            or call.status != "succeeded"
            or call.task_id is not None
            or call.task_attempt_id is not None
            or call.human_review_attempt_id is not None
            or call.invocation_index != 0
            or call.idempotency_key != f"candidate:{owner.id}"
            or call.model_input != packet.model_input.model_dump(mode="json")
            or call.model_allocation != packet.allocation.model_dump(mode="json")
            or call.context_manifest != packet.manifest.model_dump(mode="json")
            or call.finished_at is None
            or not owner.started_at <= call.reserved_at <= call.finished_at < owner.lease_expires_at
            or call.call_result is None
        ):
            raise CandidateError("candidate_call_invalid", "候选缺少同任务成功调用与真实归属")
        if call.model_usage is not None:
            usage = ReportedModelUsage.model_validate(call.model_usage)
            if (
                call.model_usage != usage.model_dump(mode="json")
                or usage.input_tokens > packet.allocation.estimated_input_tokens
                or usage.output_tokens > packet.allocation.output_token_limit
            ):
                raise CandidateError("candidate_call_invalid", "候选调用实际用量不一致或超过原分配")
        result = CandidateModelResult.model_validate(call.call_result)
        if call.call_result != result.model_dump(mode="json"):
            raise CandidateError("candidate_call_invalid", "候选调用结果表示不一致")
        validate_proposal(source, result)
        return call
    except CandidateError:
        raise
    except ValueError as error:
        raise CandidateError("candidate_integrity_violation", "候选历史材料无法规范解码") from error


async def read_job(
    session: AsyncSession,
    job: CandidateJobRow,
    run: InvestigationRunRow,
    *,
    clock: Callable[[], datetime],
) -> CandidateJobView:
    source, packet, current = await job_source(session, job, run, clock=clock)
    attempts = list(
        await session.scalars(
            select(CandidateAttemptRow)
            .where(CandidateAttemptRow.job_id == job.id)
            .order_by(CandidateAttemptRow.number)
        )
    )
    if len(attempts) != job.attempt_count or any(a.number != i + 1 for i, a in enumerate(attempts)):
        raise CandidateError("candidate_integrity_violation", "候选尝试序列不连续")
    row = await session.get(CandidateResultRow, job.id)
    result = None
    if row is not None:
        call = await validated_call(session, job, source, packet, row.source_call_id)
        result = CandidateResultView.model_validate(row, from_attributes=True)
        if (
            job.status != "succeeded"
            or result.content
            != evaluate_candidate(source, CandidateModelResult.model_validate(call.call_result))
            or row.content != result.content.model_dump(mode="json")
            or content_hash(result.content) != row.content_sha256
            or job.completed_at != row.created_at
            or not attempts
            or attempts[-1].id != row.attempt_id
            or attempts[-1].status != "succeeded"
            or attempts[-1].finished_at != row.created_at
        ):
            raise CandidateError(
                "candidate_integrity_violation", "候选结果缺少成功闭环或确定性来源"
            )
    elif job.status == "succeeded":
        raise CandidateError("candidate_integrity_violation", "成功候选缺少评估结果")
    return CandidateJobView.model_validate(
        {
            **{
                k: getattr(job, k)
                for k in (
                    "id",
                    "investigation_run_id",
                    "source_kind",
                    "source_id",
                    "source_sha256",
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
            "availability": "stale"
            if not current
            else "failed"
            if job.status == "failed"
            else "current"
            if result
            else "pending",
        },
        from_attributes=True,
    )
