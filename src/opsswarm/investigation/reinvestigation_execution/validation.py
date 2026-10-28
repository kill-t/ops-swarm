"""独立执行材料、调用归属与结果的完整历史重建。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_execution.context import build_packet, validate_report
from opsswarm.investigation.reinvestigation_execution.contracts import (
    ANALYZE,
    COLLECT,
    ExecutionModelResult,
    ExecutionPacket,
)
from opsswarm.investigation.reinvestigation_execution.source import TaskSource, validate_work
from opsswarm.investigation.reinvestigation_execution.tables import (
    ExecutionAttemptRow,
    ExecutionEvidenceRow,
    ExecutionPacketRow,
    ExecutionWorkRow,
)
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot, evidence_hash


async def validate_call(
    session: AsyncSession,
    work: ExecutionWorkRow,
    call: CallReservationRow,
    source: TaskSource,
    operation: str,
) -> None:
    owner = (
        await session.get(ExecutionAttemptRow, call.reflow_task_attempt_id)
        if call.reflow_task_attempt_id
        else None
    )
    if (
        owner is None
        or owner.task_id != work.task_id
        or owner.investigation_run_id != work.investigation_run_id
        or call.reflow_task_id != work.task_id
        or call.investigation_run_id != work.investigation_run_id
        or call.investigation_attempt_id != source.source.parent_attempt_id
        or call.reinvestigation_attempt_id is not None
        or call.candidate_attempt_id is not None
        or call.human_review_attempt_id is not None
        or call.task_id is not None
        or call.task_attempt_id is not None
        or call.operation != operation
        or call.kind != ("tool" if operation == COLLECT else "model")
        or call.idempotency_key != f"exec:{owner.id}:{operation}"
        or call.invocation_index != 0
        or call.tool_request is not None
        or not owner.started_at <= call.reserved_at < owner.lease_expires_at
        or call.reserved_at <= source.dispatch.created_at
    ):
        raise ReflowError("execution_integrity_violation", "专业调用缺少真实独立尝试与固定范围")
    if call.status == "succeeded" and (
        call.finished_at is None
        or not call.reserved_at <= call.finished_at < owner.lease_expires_at
    ):
        raise ReflowError("execution_integrity_violation", "成功调用不在原执行尝试有效期限内")
    if operation == ANALYZE:
        packet = await validated_packet(session, work, source)
        if (
            packet is None
            or call.model_input != packet.model_input.model_dump(mode="json")
            or call.model_allocation != packet.allocation.model_dump(mode="json")
            or call.context_manifest != packet.manifest.model_dump(mode="json")
        ):
            raise ReflowError("execution_integrity_violation", "专业调用未绑定完整冻结输入")
    if operation == COLLECT and any(
        x is not None
        for x in (
            call.model_input,
            call.model_allocation,
            call.model_usage,
            call.context_manifest,
            call.call_result,
        )
    ):
        raise ReflowError("execution_integrity_violation", "取证调用不能混入模型输入或结果")


async def validated_evidence(
    session: AsyncSession, work: ExecutionWorkRow, source: TaskSource
) -> TaskEvidenceSnapshot | None:
    row = await session.get(ExecutionEvidenceRow, work.task_id)
    if row is None:
        return None
    window = validate_work(work, source)
    snapshot = TaskEvidenceSnapshot.model_validate(row.snapshot)
    call = await session.get(CallReservationRow, row.call_id)
    if call is None:
        raise ReflowError("execution_integrity_violation", "专业快照缺少成功取证调用")
    await validate_call(session, work, call, source, COLLECT)
    if (
        window is None
        or row.investigation_run_id != work.investigation_run_id
        or row.attempt_id != call.reflow_task_attempt_id
        or call.status != "succeeded"
        or call.finished_at != row.created_at
        or row.snapshot_sha256 != evidence_hash(snapshot)
        or row.snapshot_sha256 != raw_content_hash(row.snapshot)
        or snapshot.version != "task-evidence-live-mcp-v1"
        or snapshot.data_source_mode != "live"
        or snapshot.evidence_snapshot_id != f"reflow-v1:{work.task_id}"
        or snapshot.role != source.assignment.role
        or snapshot.service_name != source.assignment.service_name
        or snapshot.window_start != window.window.window_start
        or snapshot.window_end != window.window.window_end
    ):
        raise ReflowError("execution_integrity_violation", "新证据快照不能由成功调用及有效窗口重建")
    return snapshot


async def validated_packet(
    session: AsyncSession, work: ExecutionWorkRow, source: TaskSource
) -> ExecutionPacket | None:
    row = await session.get(ExecutionPacketRow, work.task_id)
    if row is None:
        return None
    evidence = await validated_evidence(session, work, source)
    window = validate_work(work, source)
    if evidence is None or window is None:
        raise ReflowError("execution_integrity_violation", "专业输入缺少完整新证据")
    packet = build_packet(
        source.source, source.dispatch, source.assignment, work.task_id, window, evidence
    )
    evidence_row = await session.get(ExecutionEvidenceRow, work.task_id)
    assert evidence_row is not None
    if (
        row.created_at < evidence_row.created_at
        or row.investigation_run_id != work.investigation_run_id
        or row.packet != packet.model_dump(mode="json")
        or row.packet_sha256 != content_hash(packet)
        or row.packet_sha256 != raw_content_hash(row.packet)
    ):
        raise ReflowError("execution_integrity_violation", "专业冻结输入或材料清单无法重建")
    return packet


async def validated_model_call(
    session: AsyncSession, work: ExecutionWorkRow, call_id: UUID, source: TaskSource
) -> ExecutionModelResult:
    packet = await validated_packet(session, work, source)
    evidence = await validated_evidence(session, work, source)
    call = await session.get(CallReservationRow, call_id)
    if packet is None or evidence is None or call is None:
        raise ReflowError("execution_integrity_violation", "专业结果缺少输入、证据或真实调用")
    await validate_call(session, work, call, source, ANALYZE)
    if (
        call.status != "succeeded"
        or call.call_result is None
        or call.finished_at is None
        or call.model_input != packet.model_input.model_dump(mode="json")
        or call.context_manifest != packet.manifest.model_dump(mode="json")
        or call.model_allocation != packet.allocation.model_dump(mode="json")
    ):
        raise ReflowError("execution_integrity_violation", "专业成功调用与冻结输入不一致")
    result = ExecutionModelResult.model_validate(call.call_result)
    if (
        result.model_dump(mode="json") != call.call_result
        or call.model_usage
        and (
            call.model_usage["input_tokens"] > packet.allocation.estimated_input_tokens
            or call.model_usage["output_tokens"] > packet.allocation.output_token_limit
        )
    ):
        raise ReflowError("execution_integrity_violation", "专业成功结果或用量不满足原分配")
    validate_report(source.assignment, evidence, result.report)
    return result
