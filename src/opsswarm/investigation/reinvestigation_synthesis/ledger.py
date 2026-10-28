"""回流综合独立租约在原调用账本预留与结算，不增加第二套额度。"""

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.contracts import CallGrant, CallReservationRecord
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.budget.model_policy import summarize_model_usage
from opsswarm.investigation.budget.policy import CallBudgetExceeded, remaining_time
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.reinvestigation_synthesis.contracts import (
    SynthesisClaim,
    SynthesisError,
    SynthesisModelResult,
    SynthesisPacket,
    SynthesisSource,
)
from opsswarm.investigation.reinvestigation_synthesis.job_store import (
    _active_claim,
    _require_lease,
    utc_now,
)
from opsswarm.investigation.reinvestigation_synthesis.policy import (
    check_budget,
    validate_proposal,
)
from opsswarm.investigation.reinvestigation_synthesis.queries import validated_call
from opsswarm.investigation.reinvestigation_synthesis.source import require_current
from opsswarm.investigation.reinvestigation_synthesis.tables import SynthesisAttemptRow


class SynthesisLedger:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock

    def _now(self) -> datetime:
        return normalized_now(self._clock)

    async def reserve(self, claim: SynthesisClaim) -> CallGrant:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now(), clock=self._clock)
            source, packet = await require_current(session, run, job, clock=self._clock)
            budget = await read_budget_snapshot(session, run)
            check_budget(source, packet, budget, now=self._now(), allocate=False)
            # 成功调用可跨尝试复用，不因恢复重复预留或清零已用额度。
            successes = list(
                await session.scalars(
                    select(CallReservationRow)
                    .join(
                        SynthesisAttemptRow,
                        SynthesisAttemptRow.id == CallReservationRow.reflow_synthesis_attempt_id,
                    )
                    .where(
                        SynthesisAttemptRow.job_id == job.id,
                        CallReservationRow.status == "succeeded",
                    )
                )
            )
            if len(successes) > 1:
                raise SynthesisError(
                    "reflow_synthesis_call_conflict", "同一回流综合任务存在多个成功调用"
                )
            if successes:
                call = successes[0]
                await validated_call(session, job, call, source=source, packet=packet)
                _require_lease(job, claim.lease_token, self._now())
                return CallGrant(
                    CallReservationRecord.model_validate(call, from_attributes=True),
                    False,
                    remaining_time(source.call_budget, source.started_at, self._now()),
                )
            existing = await session.scalar(
                select(CallReservationRow.id).where(
                    CallReservationRow.reflow_synthesis_attempt_id == claim.attempt_id
                )
            )
            if existing is not None:
                raise SynthesisError(
                    "reflow_synthesis_call_already_reserved", "相同尝试不能再次发送模型请求"
                )
            check_budget(source, packet, budget, now=self._now(), allocate=True)
            now = self._now()
            _require_lease(job, claim.lease_token, now)
            call = CallReservationRow(
                id=uuid4(),
                investigation_run_id=run.id,
                investigation_attempt_id=source.parent_attempt_id,
                reflow_synthesis_attempt_id=claim.attempt_id,
                candidate_attempt_id=None,
                human_review_attempt_id=None,
                task_id=None,
                task_attempt_id=None,
                sequence=len(budget.calls) + 1,
                invocation_index=0,
                idempotency_key=f"synthesis:{claim.attempt_id}",
                operation="synthesize_reinvestigation",
                kind="model",
                status="reserved",
                error_code=None,
                reserved_at=now,
                finished_at=None,
                context_manifest=packet.manifest.model_dump(mode="json"),
                model_input=packet.model_input.model_dump(mode="json"),
                model_allocation=packet.allocation.model_dump(mode="json"),
                model_usage=None,
                tool_request=None,
                call_result=None,
            )
            session.add(call)
            await session.flush()
            _require_lease(job, claim.lease_token, self._now())
            return CallGrant(
                CallReservationRecord.model_validate(call, from_attributes=True),
                True,
                remaining_time(source.call_budget, source.started_at, self._now()),
            )

    async def check(self, claim: SynthesisClaim) -> float:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now(), clock=self._clock)
            source, packet = await require_current(session, run, job, clock=self._clock)
            own = await session.scalar(
                select(CallReservationRow).where(
                    CallReservationRow.reflow_synthesis_attempt_id == claim.attempt_id,
                    CallReservationRow.status == "reserved",
                )
            )
            if own is None:
                raise SynthesisError("reflow_synthesis_call_not_owned", "当前尝试缺少在途预留")
            _validate_frozen_call(own, claim, source, packet)
            check_budget(
                source,
                packet,
                await read_budget_snapshot(session, run),
                now=self._now(),
                allocate=False,
                own_call_id=own.id,
            )
            now = self._now()
            _require_lease(job, claim.lease_token, now)
            assert job.locked_until is not None
            available = min(
                remaining_time(source.call_budget, source.started_at, now),
                (job.locked_until - now).total_seconds() - 1,
                120.0,
            )
            if available <= 0:
                raise SynthesisError("reflow_synthesis_lease_lost", "租约不足以结算模型请求")
            return available

    async def settle(
        self,
        claim: SynthesisClaim,
        call_id: UUID,
        *,
        status: Literal["succeeded", "failed", "unknown"],
        usage: ReportedModelUsage | None,
        result: SynthesisModelResult | None = None,
    ) -> bool:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now(), clock=self._clock)
            call = await session.get(CallReservationRow, call_id)
            if (
                call is None
                or call.reflow_synthesis_attempt_id != claim.attempt_id
                or call.investigation_run_id != run.id
            ):
                raise SynthesisError(
                    "reflow_synthesis_call_not_owned", "调用不属于当前回流综合尝试"
                )
            frozen_source = SynthesisSource.model_validate(job.source)
            frozen_packet = SynthesisPacket.model_validate(job.packet)
            _validate_frozen_call(call, claim, frozen_source, frozen_packet)
            # 即使返回失败也核验冻结材料；取消后只保留保守占用和已知用量。
            try:
                source, packet = await require_current(session, run, job, clock=self._clock)
                check_budget(
                    source,
                    packet,
                    await read_budget_snapshot(session, run),
                    now=self._now(),
                    allocate=False,
                    own_call_id=call.id,
                )
            except HumanWaitError:
                status, result = "unknown", None
            except SynthesisError as error:
                if error.code not in ("reflow_source_changed", "run_deadline_exceeded"):
                    raise
                status, result = "unknown", None
            except CallBudgetExceeded:
                status, result = "unknown", None
            if status == "succeeded":
                if result is None:
                    raise SynthesisError(
                        "reflow_synthesis_result_required", "成功调用必须包含完整提案"
                    )
                validate_proposal(source, result.proposal)
                budget = await read_budget_snapshot(session, run)
                calls = tuple(
                    item.model_copy(update={"model_usage": usage, "status": "succeeded"})
                    if item.id == call.id
                    else item
                    for item in budget.calls
                )
                projected = budget.model_copy(
                    update={
                        "calls": calls,
                        "model_usage": summarize_model_usage(
                            source.model_budget,
                            [
                                (item.model_allocation, item.model_usage)
                                for item in calls
                                if item.kind == "model" and item.model_allocation is not None
                            ],
                        ),
                    }
                )
                try:
                    check_budget(source, packet, projected, now=self._now(), allocate=False)
                except CallBudgetExceeded:
                    status, result = "unknown", None
            elif result is not None:
                raise SynthesisError(
                    "reflow_synthesis_result_invalid", "非成功调用不能保存回流综合提案"
                )
            data = result.model_dump(mode="json") if result else None
            usage_data = usage.model_dump(mode="json") if usage else None
            error_code = (
                None
                if status == "succeeded"
                else "operation_failed"
                if status == "failed"
                else "operation_cancelled"
            )
            if call.status != "reserved":
                if (call.status, call.model_usage, call.call_result, call.error_code) != (
                    status,
                    usage_data,
                    data,
                    error_code,
                ):
                    raise SynthesisError("reflow_synthesis_settlement_conflict", "调用已有其他结算")
            else:
                call.status, call.finished_at, call.error_code = status, self._now(), error_code
                call.model_usage, call.call_result = usage_data, data
            await session.flush()
            _require_lease(job, claim.lease_token, self._now())
            if status == "succeeded":
                remaining_time(source.call_budget, source.started_at, self._now())
            return status == "succeeded"


def _validate_frozen_call(
    call: CallReservationRow,
    claim: SynthesisClaim,
    source: SynthesisSource,
    packet: SynthesisPacket,
) -> None:
    if (
        call.investigation_run_id != claim.investigation_run_id
        or call.reflow_synthesis_attempt_id != claim.attempt_id
        or call.operation != "synthesize_reinvestigation"
        or call.kind != "model"
        or call.investigation_attempt_id != source.parent_attempt_id
        or call.candidate_attempt_id is not None
        or call.reinvestigation_attempt_id is not None
        or call.reflow_task_id is not None
        or call.reflow_task_attempt_id is not None
        or call.human_review_attempt_id is not None
        or call.task_id is not None
        or call.task_attempt_id is not None
        or call.tool_request is not None
        or call.invocation_index != 0
        or call.idempotency_key != f"synthesis:{claim.attempt_id}"
        or call.model_input != packet.model_input.model_dump(mode="json")
        or call.model_allocation != packet.allocation.model_dump(mode="json")
        or call.context_manifest != packet.manifest.model_dump(mode="json")
    ):
        raise SynthesisError("reflow_synthesis_call_invalid", "回流综合调用归属或冻结输入不一致")
