"""候选独立租约在原调用账本预留与结算，不增加第二套额度。"""

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
from opsswarm.investigation.candidate.budget import check_budget
from opsswarm.investigation.candidate.contracts import (
    CandidateClaim,
    CandidateError,
    CandidatePacket,
    CandidateSource,
)
from opsswarm.investigation.candidate.evaluation import validate_proposal
from opsswarm.investigation.candidate.job_store import (
    _active_claim,
    _current_packet,
    _require_lease,
)
from opsswarm.investigation.candidate.output import CandidateModelResult
from opsswarm.investigation.candidate.queries import validated_call
from opsswarm.investigation.candidate.store import utc_now
from opsswarm.investigation.candidate.tables import CandidateAttemptRow
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.persistence.lease import normalized_now


class CandidateLedger:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock

    def _now(self) -> datetime:
        return normalized_now(self._clock)

    async def reserve(self, claim: CandidateClaim) -> CallGrant:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now())
            source, packet = await _current_packet(session, run, job, clock=self._clock)
            budget = await read_budget_snapshot(session, run)
            check_budget(source, packet, budget, now=self._now(), allocate=False)
            # 成功调用可跨尝试复用，不因恢复重复预留或清零已用额度。
            successes = list(
                await session.scalars(
                    select(CallReservationRow)
                    .join(
                        CandidateAttemptRow,
                        CandidateAttemptRow.id == CallReservationRow.candidate_attempt_id,
                    )
                    .where(
                        CandidateAttemptRow.job_id == job.id,
                        CallReservationRow.status == "succeeded",
                    )
                )
            )
            if len(successes) > 1:
                raise CandidateError("candidate_call_conflict", "同一候选任务存在多个成功调用")
            if successes:
                call = await validated_call(session, job, source, packet, successes[0].id)
                _require_lease(job, claim.lease_token, self._now())
                return CallGrant(
                    CallReservationRecord.model_validate(call, from_attributes=True),
                    False,
                    remaining_time(source.call_budget, source.started_at, self._now()),
                )
            existing = await session.scalar(
                select(CallReservationRow.id).where(
                    CallReservationRow.candidate_attempt_id == claim.attempt_id
                )
            )
            if existing is not None:
                raise CandidateError(
                    "candidate_call_already_reserved", "相同尝试不能再次发送模型请求"
                )
            check_budget(source, packet, budget, now=self._now(), allocate=True)
            now = self._now()
            _require_lease(job, claim.lease_token, now)
            call = CallReservationRow(
                id=uuid4(),
                investigation_run_id=run.id,
                investigation_attempt_id=source.handoff.investigation_attempt_id,
                candidate_attempt_id=claim.attempt_id,
                human_review_attempt_id=None,
                task_id=None,
                task_attempt_id=None,
                sequence=len(budget.calls) + 1,
                invocation_index=0,
                idempotency_key=f"candidate:{claim.attempt_id}",
                operation="evaluate_candidate",
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

    async def check(self, claim: CandidateClaim) -> float:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now())
            source, packet = await _current_packet(session, run, job, clock=self._clock)
            check_budget(
                source,
                packet,
                await read_budget_snapshot(session, run),
                now=self._now(),
                allocate=False,
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
                raise CandidateError("candidate_lease_lost", "租约不足以结算模型请求")
            return available

    async def settle(
        self,
        claim: CandidateClaim,
        call_id: UUID,
        *,
        status: Literal["succeeded", "failed", "unknown"],
        usage: ReportedModelUsage | None,
        result: CandidateModelResult | None = None,
    ) -> bool:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now())
            call = await session.get(CallReservationRow, call_id)
            if (
                call is None
                or call.candidate_attempt_id != claim.attempt_id
                or call.investigation_run_id != run.id
            ):
                raise CandidateError("candidate_call_not_owned", "调用不属于当前候选尝试")
            frozen_source = CandidateSource.model_validate(job.source)
            frozen_packet = CandidatePacket.model_validate(job.packet)
            if (
                call.operation != "evaluate_candidate"
                or call.kind != "model"
                or call.investigation_attempt_id != frozen_source.handoff.investigation_attempt_id
                or call.human_review_attempt_id is not None
                or call.task_id is not None
                or call.task_attempt_id is not None
                or call.invocation_index != 0
                or call.idempotency_key != f"candidate:{claim.attempt_id}"
                or call.model_input != frozen_packet.model_input.model_dump(mode="json")
                or call.model_allocation != frozen_packet.allocation.model_dump(mode="json")
                or call.context_manifest != frozen_packet.manifest.model_dump(mode="json")
            ):
                raise CandidateError("candidate_call_invalid", "候选调用归属或冻结输入不一致")
            # 即使返回失败也核验冻结材料；取消后只保留保守占用和已知用量。
            try:
                source, packet = await _current_packet(session, run, job, clock=self._clock)
                check_budget(
                    source,
                    packet,
                    await read_budget_snapshot(session, run),
                    now=self._now(),
                    allocate=False,
                )
            except HumanWaitError:
                status, result = "unknown", None
            except CandidateError as error:
                if error.code != "candidate_source_changed":
                    raise
                status, result = "unknown", None
            except CallBudgetExceeded:
                status, result = "unknown", None
            if status == "succeeded":
                if result is None:
                    raise CandidateError("candidate_result_required", "成功调用必须包含完整提案")
                validate_proposal(source, result)
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
                raise CandidateError("candidate_result_invalid", "非成功调用不能保存候选提案")
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
                    raise CandidateError("candidate_settlement_conflict", "调用已有其他结算")
            else:
                call.status, call.finished_at, call.error_code = status, self._now(), error_code
                call.model_usage, call.call_result = usage_data, data
            await session.flush()
            _require_lease(job, claim.lease_token, self._now())
            if status == "succeeded":
                remaining_time(source.call_budget, source.started_at, self._now())
            return status == "succeeded"
