"""独立重评租约在原调用账本中预留与结算，不新建额度。"""

from collections.abc import Callable
from datetime import datetime
from typing import Literal
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.investigation.budget.contracts import CallGrant, CallReservationRecord
from opsswarm.investigation.budget.model_contracts import ReportedModelUsage
from opsswarm.investigation.budget.policy import CallBudgetExceeded, remaining_time
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.budget.tables import CallReservationRow
from opsswarm.investigation.human_prepare.builder import diagnose_preparation
from opsswarm.investigation.human_review.contracts import HumanReviewClaim, HumanReviewError
from opsswarm.investigation.human_review.job_store import (
    _active_claim,
    _require_current,
    _require_lease,
)
from opsswarm.investigation.human_review.policy import require_committed_capacity
from opsswarm.investigation.human_review.queries import job_source, validate_result, validated_call
from opsswarm.investigation.human_review.store import utc_now
from opsswarm.investigation.human_review.tables import HumanReviewAttemptRow
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.persistence.lease import normalized_now
from opsswarm.investigation.review.contracts import ReviewModelResult


class HumanReviewLedger:
    def __init__(
        self, factory: async_sessionmaker[AsyncSession], *, clock: Callable[[], datetime] = utc_now
    ) -> None:
        self._factory, self._clock = factory, clock

    def _now(self) -> datetime:
        return normalized_now(self._clock)

    async def reserve(self, claim: HumanReviewClaim) -> CallGrant:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now())
            source, artifact, _ = await job_source(session, job, run, clock=self._clock)
            await _require_current(session, run, job)
            remaining_time(source.call_budget, source.started_at, self._now())
            # 已成功请求优先复用；恢复不会因已用额度而重复调用或创建新预算。
            successes = list(
                await session.scalars(
                    select(CallReservationRow)
                    .join(
                        HumanReviewAttemptRow,
                        HumanReviewAttemptRow.id == CallReservationRow.human_review_attempt_id,
                    )
                    .where(
                        HumanReviewAttemptRow.job_id == job.id,
                        CallReservationRow.status == "succeeded",
                    )
                )
            )
            if len(successes) > 1:
                raise HumanReviewError("human_review_call_conflict", "同一重评存在多个成功调用")
            if successes:
                call = await validated_call(session, job, source, artifact, successes[0].id)
                _require_lease(job, claim.lease_token, self._now())
                return CallGrant(
                    CallReservationRecord.model_validate(call, from_attributes=True),
                    False,
                    remaining_time(source.call_budget, source.started_at, self._now()),
                )
            existing = await session.scalar(
                select(CallReservationRow.id).where(
                    CallReservationRow.human_review_attempt_id == claim.attempt_id
                )
            )
            if existing is not None:
                raise HumanReviewError(
                    "human_review_call_already_reserved", "相同尝试预留不能再次发送模型请求"
                )
            budget = await read_budget_snapshot(session, run)
            diagnostic = diagnose_preparation(
                source, artifact.prepared_input, budget, now=self._now()
            )
            if diagnostic.status != "ready":
                raise HumanReviewError(
                    diagnostic.reason_code or "human_review_not_ready", "原预算不允许重评调用"
                )
            prepared = artifact.prepared_input
            assert prepared.model_input is not None and prepared.allocation is not None
            now = self._now()
            _require_lease(job, claim.lease_token, now)
            call = CallReservationRow(
                id=uuid4(),
                investigation_run_id=run.id,
                investigation_attempt_id=source.handoff.investigation_attempt_id,
                human_review_attempt_id=claim.attempt_id,
                task_id=None,
                task_attempt_id=None,
                sequence=len(budget.calls) + 1,
                invocation_index=0,
                idempotency_key=f"human-review:{claim.attempt_id}",
                operation="human_review",
                kind="model",
                status="reserved",
                error_code=None,
                reserved_at=now,
                finished_at=None,
                context_manifest=prepared.manifest.model_dump(mode="json"),
                model_input=prepared.model_input.model_dump(mode="json"),
                model_allocation=prepared.allocation.model_dump(mode="json"),
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

    async def check(self, claim: HumanReviewClaim) -> float:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now())
            source, _, _ = await job_source(session, job, run, clock=self._clock)
            await _require_current(session, run, job)
            budget = await read_budget_snapshot(session, run)
            require_committed_capacity(budget)
            now = self._now()
            _require_lease(job, claim.lease_token, now)
            assert job.locked_until is not None
            available = min(
                remaining_time(source.call_budget, source.started_at, now),
                (job.locked_until - now).total_seconds() - 1,
                120.0,
            )
            if available <= 0:
                raise HumanReviewError("human_review_lease_lost", "租约不足以结算模型请求")
            return available

    async def settle(
        self,
        claim: HumanReviewClaim,
        call_id: UUID,
        *,
        status: Literal["succeeded", "failed", "unknown"],
        usage: ReportedModelUsage | None,
        result: ReviewModelResult | None = None,
    ) -> bool:
        async with self._factory() as session, session.begin():
            job, _, run = await _active_claim(session, claim, self._now())
            call = await session.get(CallReservationRow, call_id)
            if (
                call is None
                or call.human_review_attempt_id != claim.attempt_id
                or call.investigation_run_id != run.id
            ):
                raise HumanReviewError("human_review_call_not_owned", "调用不属于当前重评尝试")
            accepted = status == "succeeded"
            if accepted:
                try:
                    source, _, _ = await job_source(session, job, run, clock=self._clock)
                    await _require_current(session, run, job)
                    remaining_time(source.call_budget, source.started_at, self._now())
                    if result is None:
                        raise HumanReviewError(
                            "human_review_result_required", "成功调用必须包含完整意见"
                        )
                    validate_result(source, result)
                except HumanWaitError:
                    accepted = False
                except HumanReviewError as error:
                    if error.code != "human_review_wait_changed":
                        raise
                    accepted = False
                except CallBudgetExceeded:
                    accepted = False
                if not accepted:
                    status, result = "unknown", None
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
                    raise HumanReviewError("human_review_settlement_conflict", "调用已有其他结算")
            else:
                call.status, call.finished_at, call.error_code = status, self._now(), error_code
                call.model_usage, call.call_result = usage_data, data
            await session.flush()
            _require_lease(job, claim.lease_token, self._now())
            if accepted:
                remaining_time(source.call_budget, source.started_at, self._now())
            return accepted
