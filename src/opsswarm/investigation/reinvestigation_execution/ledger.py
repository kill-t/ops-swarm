"""以真实任务尝试在原账本预留并保存新证据和结构化调用。"""

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.execution.release.observer import utc_now
from opsswarm.investigation.budget.contracts import CallGrant, CallReservationRecord
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.budget.policy import CallBudgetExceeded, remaining_time
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_execution.context import build_packet, validate_report
from opsswarm.investigation.reinvestigation_execution.contracts import (
    ANALYZE,
    COLLECT,
    ExecutionClaim,
    ExecutionModelResult,
    ExecutionPacket,
)
from opsswarm.investigation.reinvestigation_execution.policy import check_budget
from opsswarm.investigation.reinvestigation_execution.source import require_current, validate_work
from opsswarm.investigation.reinvestigation_execution.store import active_claim, require_lease
from opsswarm.investigation.reinvestigation_execution.tables import (
    ExecutionEvidenceRow,
    ExecutionPacketRow,
)
from opsswarm.investigation.reinvestigation_execution.validation import (
    validate_call,
    validated_evidence,
    validated_model_call,
    validated_packet,
)
from opsswarm.investigation.tasks.evidence import TaskEvidenceSnapshot, evidence_hash


class ExecutionLedger:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ):
        self._factory, self._clock = factory, clock

    async def reserve(self, claim: ExecutionClaim, operation: str) -> CallGrant:
        if operation not in (COLLECT, ANALYZE):
            raise ValueError("专业调用只能是固定取证或专业分析")
        async with self._factory() as session, session.begin():
            work, _, run = await active_claim(session, claim, clock=self._clock)
            source = await require_current(session, run, work, clock=self._clock)
            budget = await read_budget_snapshot(session, run)
            check_budget(
                source.source, budget, task_id=work.task_id, now=normalized_now(self._clock)
            )
            successes = list(
                await session.scalars(
                    select(CallReservationRow).where(
                        CallReservationRow.reflow_task_id == work.task_id,
                        CallReservationRow.operation == operation,
                        CallReservationRow.status == "succeeded",
                    )
                )
            )
            if len(successes) > 1:
                raise ReflowError("execution_integrity_violation", "同任务固定阶段存在多个成功调用")
            if successes:
                call = successes[0]
                if operation == COLLECT:
                    if await validated_evidence(session, work, source) is None:
                        raise ReflowError(
                            "execution_integrity_violation", "成功取证没有原子保存快照"
                        )
                else:
                    await validated_model_call(session, work, call.id, source)
                require_lease(work, claim.lease_token, normalized_now(self._clock))
                return CallGrant(
                    CallReservationRecord.model_validate(call, from_attributes=True),
                    False,
                    remaining_time(
                        source.source.call_budget,
                        source.source.started_at,
                        normalized_now(self._clock),
                    ),
                )
            existing = await session.scalar(
                select(CallReservationRow.id).where(
                    CallReservationRow.reflow_task_attempt_id == claim.attempt_id,
                    CallReservationRow.operation == operation,
                )
            )
            if existing is not None:
                raise ReflowError("execution_call_already_reserved", "相同尝试的阶段调用不能重发")
            packet = await validated_packet(session, work, source) if operation == ANALYZE else None
            if operation == ANALYZE and packet is None:
                raise ReflowError(
                    "execution_integrity_violation", "模型预留需要已冻结的完整专业输入"
                )
            check_budget(
                source.source,
                budget,
                task_id=work.task_id,
                now=normalized_now(self._clock),
                reserve=operation,
                allocation=packet.allocation if packet else None,
            )
            now = normalized_now(self._clock)
            require_lease(work, claim.lease_token, now)
            call = CallReservationRow(
                id=uuid4(),
                investigation_run_id=run.id,
                investigation_attempt_id=source.source.parent_attempt_id,
                reflow_task_id=work.task_id,
                reflow_task_attempt_id=claim.attempt_id,
                reinvestigation_attempt_id=None,
                candidate_attempt_id=None,
                human_review_attempt_id=None,
                task_id=None,
                task_attempt_id=None,
                sequence=len(budget.calls) + 1,
                invocation_index=0,
                idempotency_key=f"exec:{claim.attempt_id}:{operation}",
                operation=operation,
                kind="tool" if operation == COLLECT else "model",
                status="reserved",
                error_code=None,
                reserved_at=now,
                finished_at=None,
                tool_request=None,
                call_result=None,
                model_input=packet.model_input.model_dump(mode="json") if packet else None,
                model_allocation=packet.allocation.model_dump(mode="json") if packet else None,
                context_manifest=packet.manifest.model_dump(mode="json") if packet else None,
                model_usage=None,
            )
            session.add(call)
            await session.flush()
            require_lease(work, claim.lease_token, normalized_now(self._clock))
            return CallGrant(
                CallReservationRecord.model_validate(call, from_attributes=True),
                True,
                remaining_time(
                    source.source.call_budget, source.source.started_at, normalized_now(self._clock)
                ),
            )

    async def check(self, claim: ExecutionClaim) -> float:
        async with self._factory() as session, session.begin():
            work, _, run = await active_claim(session, claim, clock=self._clock)
            source = await require_current(session, run, work, clock=self._clock)
            check_budget(
                source.source,
                await read_budget_snapshot(session, run),
                task_id=work.task_id,
                now=normalized_now(self._clock),
            )
            now = normalized_now(self._clock)
            require_lease(work, claim.lease_token, now)
            assert work.locked_until is not None
            return max(
                0,
                min(
                    120.0,
                    (work.locked_until - now).total_seconds() - 1,
                    remaining_time(source.source.call_budget, source.source.started_at, now),
                ),
            )

    async def save_evidence(
        self, claim: ExecutionClaim, call_id: UUID, snapshot: TaskEvidenceSnapshot
    ) -> bool:
        async with self._factory() as session, session.begin():
            work, _, run = await active_claim(session, claim, clock=self._clock)
            source = await require_current(session, run, work, clock=self._clock)
            window = validate_work(work, source)
            assert window is not None
            if (
                snapshot.version != "task-evidence-live-mcp-v1"
                or snapshot.data_source_mode != "live"
                or snapshot.evidence_snapshot_id != f"reflow-v1:{work.task_id}"
                or snapshot.role != source.assignment.role
                or snapshot.service_name != source.assignment.service_name
                or snapshot.window_start != window.window.window_start
                or snapshot.window_end != window.window.window_end
            ):
                raise ReflowError(
                    "execution_evidence_invalid", "采集材料不属于固定角色、服务及新窗口"
                )
            call = await session.get(CallReservationRow, call_id)
            if call is None or call.reflow_task_attempt_id != claim.attempt_id:
                raise ReflowError("execution_call_not_owned", "取证调用不属于当前尝试")
            await validate_call(session, work, call, source, COLLECT)
            if (
                call.status != "reserved"
                or await session.get(ExecutionEvidenceRow, work.task_id) is not None
            ):
                raise ReflowError("execution_integrity_violation", "取证成功快照不能重复发布")
            check_budget(
                source.source,
                await read_budget_snapshot(session, run),
                task_id=work.task_id,
                now=normalized_now(self._clock),
            )
            now = normalized_now(self._clock)
            call.status, call.finished_at = "succeeded", now
            await session.flush()
            session.add(
                ExecutionEvidenceRow(
                    task_id=work.task_id,
                    investigation_run_id=run.id,
                    attempt_id=claim.attempt_id,
                    call_id=call.id,
                    snapshot=snapshot.model_dump(mode="json"),
                    snapshot_sha256=evidence_hash(snapshot),
                    created_at=now,
                )
            )
            await session.flush()
            require_lease(work, claim.lease_token, normalized_now(self._clock))
            return True

    async def prepare_packet(self, claim: ExecutionClaim) -> ExecutionPacket:
        async with self._factory() as session, session.begin():
            work, _, run = await active_claim(session, claim, clock=self._clock)
            source = await require_current(session, run, work, clock=self._clock)
            saved = await validated_packet(session, work, source)
            if saved is not None:
                return saved
            evidence = await validated_evidence(session, work, source)
            window = validate_work(work, source)
            if evidence is None or window is None:
                raise ReflowError("execution_integrity_violation", "专业上下文缺少成功取证或窗口")
            packet = build_packet(
                source.source, source.dispatch, source.assignment, work.task_id, window, evidence
            )
            session.add(
                ExecutionPacketRow(
                    task_id=work.task_id,
                    investigation_run_id=run.id,
                    packet=packet.model_dump(mode="json"),
                    packet_sha256=content_hash(packet),
                    created_at=normalized_now(self._clock),
                )
            )
            await session.flush()
            require_lease(work, claim.lease_token, normalized_now(self._clock))
            return packet

    async def settle(
        self,
        claim: ExecutionClaim,
        call_id: UUID,
        *,
        status: Literal["succeeded", "failed", "unknown"],
        usage: ReportedModelUsage | None = None,
        result: ExecutionModelResult | None = None,
    ) -> bool:
        async with self._factory() as session, session.begin():
            work, _, run = await active_claim(session, claim, clock=self._clock)
            call = await session.get(CallReservationRow, call_id)
            if (
                call is None
                or call.reflow_task_attempt_id != claim.attempt_id
                or call.reflow_task_id != claim.task_id
            ):
                raise ReflowError("execution_call_not_owned", "调用不属于当前专业尝试")
            # 来源变化后只结算unknown审计，保留实际用量及保守占用。
            from opsswarm.investigation.reinvestigation_execution.source import load_source

            source = await load_source(session, run, work.task_id, clock=self._clock)
            await validate_call(session, work, call, source, call.operation)
            try:
                await require_current(session, run, work, clock=self._clock)
                check_budget(
                    source.source,
                    await read_budget_snapshot(session, run),
                    task_id=work.task_id,
                    now=normalized_now(self._clock),
                )
            except (CallBudgetExceeded, ReflowError) as error:
                if isinstance(error, ReflowError) and error.code not in (
                    "reflow_source_changed",
                    "run_deadline_exceeded",
                ):
                    raise
                status, result = "unknown", None
            if status == "succeeded":
                packet = await validated_packet(session, work, source)
                evidence = await validated_evidence(session, work, source)
                if (
                    call.operation != ANALYZE
                    or packet is None
                    or evidence is None
                    or result is None
                ):
                    raise ReflowError(
                        "execution_result_required", "成功模型调用必须包含完整专业报告"
                    )
                validate_report(source.assignment, evidence, result.report)
                if usage and (
                    usage.input_tokens > packet.allocation.estimated_input_tokens
                    or usage.output_tokens > packet.allocation.output_token_limit
                ):
                    status, result = "unknown", None
            elif result is not None:
                raise ReflowError("execution_result_invalid", "非成功调用不能保存专业报告")
            if call.status != "reserved":
                raise ReflowError("execution_settlement_conflict", "专业调用已经结算")
            call.status, call.finished_at = status, normalized_now(self._clock)
            call.model_usage = usage.model_dump(mode="json") if usage else None
            call.call_result = result.model_dump(mode="json") if result else None
            call.error_code = (
                None
                if status == "succeeded"
                else "operation_failed"
                if status == "failed"
                else "operation_cancelled"
            )
            await session.flush()
            require_lease(work, claim.lease_token, normalized_now(self._clock))
            return status == "succeeded"
