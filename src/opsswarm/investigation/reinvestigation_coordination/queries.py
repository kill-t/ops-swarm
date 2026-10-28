"""回流输入、尝试和成功建议的完整历史读取。"""

from collections.abc import Callable
from datetime import datetime
from typing import Literal, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.execution.release.observer import utc_now
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.human_wait.state import lock_wait_run
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation.tables import CandidateReinvestigationRow
from opsswarm.investigation.reinvestigation_coordination.context import build_packet
from opsswarm.investigation.reinvestigation_coordination.contracts import (
    POLICY_VERSION,
    ReflowAttempt,
    ReflowError,
    ReflowModelResult,
    ReflowPacket,
    ReflowResult,
    ReflowSource,
    ReflowView,
)
from opsswarm.investigation.reinvestigation_coordination.policy import validate_proposal
from opsswarm.investigation.reinvestigation_coordination.source import rebuild_source
from opsswarm.investigation.reinvestigation_coordination.tables import (
    ReflowAttemptRow,
    ReflowJobRow,
    ReflowResultRow,
)


async def validate_call_binding(
    session: AsyncSession,
    job: ReflowJobRow,
    call: CallReservationRow,
    *,
    source: ReflowSource,
    packet: ReflowPacket,
) -> None:
    owner = await session.get(ReflowAttemptRow, call.reinvestigation_attempt_id)
    if (
        owner is None
        or owner.job_id != job.id
        or owner.investigation_run_id != job.investigation_run_id
        or call.investigation_run_id != job.investigation_run_id
        or call.investigation_attempt_id != source.parent_attempt_id
        or call.operation != "coordinate_reinvestigation"
        or call.kind != "model"
        or call.human_review_attempt_id is not None
        or call.candidate_attempt_id is not None
        or call.task_id is not None
        or call.task_attempt_id is not None
        or call.invocation_index != 0
        or call.idempotency_key != f"reflow:{owner.id}"
        or call.reserved_at < owner.started_at
        or call.reserved_at >= owner.lease_expires_at
        or call.model_input != packet.model_input.model_dump(mode="json")
        or call.model_allocation != packet.allocation.model_dump(mode="json")
        or call.context_manifest != packet.manifest.model_dump(mode="json")
        or call.tool_request is not None
    ):
        raise ReflowError("reflow_integrity_violation", "回流调用缺少精确阶段归属与冻结输入")


async def validated_call(
    session: AsyncSession,
    job: ReflowJobRow,
    call: CallReservationRow,
    *,
    source: ReflowSource,
    packet: ReflowPacket,
) -> ReflowModelResult:
    await validate_call_binding(session, job, call, source=source, packet=packet)
    if call.status != "succeeded" or call.call_result is None or call.finished_at is None:
        raise ReflowError("reflow_integrity_violation", "回流成功建议缺少已结算的模型调用")
    result = ReflowModelResult.model_validate(call.call_result)
    if result.model_dump(mode="json") != call.call_result:
        raise ReflowError("reflow_integrity_violation", "回流调用结果不是规范结构")
    if call.model_usage and (
        call.model_usage["input_tokens"] > packet.allocation.estimated_input_tokens
        or call.model_usage["output_tokens"] > packet.allocation.output_token_limit
    ):
        raise ReflowError("reflow_integrity_violation", "超原分配的结果不能成为成功建议")
    validate_proposal(source, result.proposal)
    return result


async def frozen_packet(
    session: AsyncSession,
    run: InvestigationRunRow,
    job: ReflowJobRow,
    *,
    clock: Callable[[], datetime],
) -> tuple[ReflowSource, ReflowPacket]:
    admission = await session.get(CandidateReinvestigationRow, run.id)
    if admission is None or job.source is None or job.packet is None:
        raise ReflowError("reflow_integrity_violation", "回流没有完整冻结输入")
    source = await rebuild_source(session, run, admission, created_at=job.created_at, clock=clock)
    packet = build_packet(source)
    if (
        source.model_dump(mode="json") != job.source
        or content_hash(source) != job.source_sha256
        or raw_content_hash(job.source) != job.source_sha256
        or packet.model_dump(mode="json") != job.packet
        or content_hash(packet) != job.packet_sha256
        or raw_content_hash(job.packet) != job.packet_sha256
    ):
        raise ReflowError("reflow_integrity_violation", "回流历史输入或来源摘要无法重建")
    return source, packet


async def read_job(
    session: AsyncSession,
    run: InvestigationRunRow,
    job: ReflowJobRow | None,
    *,
    clock: Callable[[], datetime],
) -> ReflowView:
    if job is None:
        return ReflowView(
            job_id=None,
            status="pending",
            source_sha256=None,
            packet=None,
            attempts=(),
            result=None,
            error_code=None,
        )
    try:
        if (
            job.id != run.id
            or job.investigation_run_id != run.id
            or job.policy_version != POLICY_VERSION
        ):
            raise ValueError("回流阶段身份或版本不一致")
        attempts = list(
            await session.scalars(
                select(ReflowAttemptRow)
                .where(ReflowAttemptRow.job_id == job.id)
                .order_by(ReflowAttemptRow.number)
            )
        )
        if len(attempts) != job.attempt_count or any(
            a.number != i for i, a in enumerate(attempts, 1)
        ):
            raise ValueError("回流尝试序列不连续")
        packet = None
        source = None
        if job.packet is not None:
            source, packet = await frozen_packet(session, run, job, clock=clock)
        elif job.status != "blocked" or not job.last_error:
            raise ValueError("可调用阶段缺少冻结输入")
        elif job.source is not None:
            admission = await session.get(CandidateReinvestigationRow, run.id)
            assert admission is not None
            captured = await rebuild_source(
                session, run, admission, created_at=job.created_at, clock=clock
            )
            if (
                captured.model_dump(mode="json") != job.source
                or content_hash(captured) != job.source_sha256
            ):
                raise ValueError("阻断阶段来源发生漂移")
        else:
            admission = await session.get(CandidateReinvestigationRow, run.id)
            if admission is None or job.source_sha256 is not None or job.packet_sha256 is not None:
                raise ValueError("无输入阻断阶段缺少准入或残留摘要")
            # 当前来源变化可以阻断准备，但不免除对原准入历史的完整性核验。
            await rebuild_source(session, run, admission, created_at=job.created_at, clock=clock)
        stored = await session.get(ReflowResultRow, job.id)
        result = None
        if stored is not None:
            if source is None or packet is None:
                raise ValueError("成功结果缺少输入")
            result = ReflowResult.model_validate(stored.content)
            call = await session.get(CallReservationRow, stored.source_call_id)
            if call is None:
                raise ValueError("成功建议调用缺失")
            model_result = await validated_call(session, job, call, source=source, packet=packet)
            if (
                result.job_id != job.id
                or result.investigation_run_id != run.id
                or result.source_call_id != call.id
                or result.source_sha256 != job.source_sha256
                or result.proposal != model_result.proposal
                or result.created_at != stored.created_at
                or stored.content_sha256 != content_hash(result)
                or stored.content_sha256 != raw_content_hash(stored.content)
                or job.status != "succeeded"
                or job.completed_at != result.created_at
                or not attempts
                or attempts[-1].id != stored.attempt_id
                or attempts[-1].status != "succeeded"
                or attempts[-1].finished_at != result.created_at
            ):
                raise ValueError("回流结果与结算尝试不一致")
        elif job.status == "succeeded":
            raise ValueError("成功阶段缺少持久建议")
        return ReflowView(
            job_id=job.id,
            status=cast(Literal["queued", "running", "succeeded", "blocked", "failed"], job.status),
            source_sha256=job.source_sha256,
            packet=packet,
            attempts=tuple(ReflowAttempt.model_validate(a, from_attributes=True) for a in attempts),
            result=result,
            error_code=job.last_error,
        )
    except (ValueError, TypeError) as error:
        raise ReflowError("reflow_integrity_violation", "回流阶段历史无法重建") from error


class ReflowQueries:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ):
        self._factory, self._clock = factory, clock

    async def get(self, run_id: UUID) -> ReflowView:
        async with self._factory() as session, session.begin():
            run = await lock_wait_run(session, run_id, read=True)
            return await read_job(
                session, run, await session.get(ReflowJobRow, run_id), clock=self._clock
            )
