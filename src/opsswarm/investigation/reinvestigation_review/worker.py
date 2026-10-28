"""在持久预留之外执行有界模型请求，独立结算阶段结果。"""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from uuid import UUID

from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.human_prepare.contracts import PreparationError
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.reinvestigation_review.contracts import ReflowReviewError
from opsswarm.investigation.reinvestigation_review.job_store import ReflowReviewJobStore
from opsswarm.investigation.reinvestigation_review.ledger import ReflowReviewLedger
from opsswarm.investigation.reinvestigation_review.model import ReflowReviewModel
from opsswarm.investigation.tasks.errors import TaskFactConflict


class ReflowReviewWorker:
    def __init__(
        self,
        store: ReflowReviewJobStore,
        ledger: ReflowReviewLedger,
        *,
        model_factory: Callable[[ModelBudgetPolicy], ReflowReviewModel],
        worker_id: str,
    ) -> None:
        self._store, self._ledger = store, ledger
        self._factory, self._worker_id = model_factory, worker_id

    async def run_once(self) -> bool:
        prepared = await self._store.prepare_next()
        claim = await self._store.claim_next(worker_id=self._worker_id)
        if claim is None:
            return prepared
        client = None
        call_id: UUID | None = None
        settled = False
        try:
            source, packet = await self._store.get_packet(claim)
            # 装配/配置校验在预留前完成，不因错误模型配置浪费调用额度。
            client = self._factory(source.model_budget)
            grant = await self._ledger.reserve(claim)
            call_id = grant.record.id
            if grant.created:
                timeout = await self._ledger.check(claim)
                model_input = packet.model_input
                async with asyncio.timeout(timeout):
                    result = await client.invoke(model_input)
                accepted = await self._ledger.settle(
                    claim, call_id, status="succeeded", result=result, usage=client.usage
                )
                settled = True
                if not accepted:
                    raise ReflowReviewError(
                        "reflow_review_source_changed", "输入、取消或时限改变后不能发布意见"
                    )
            else:
                settled = True
            await self._store.complete(claim, call_id)
        except (Exception, asyncio.CancelledError) as error:
            lease_lost = (
                isinstance(error, ReflowReviewError) and error.code == "reflow_review_lease_lost"
            )
            if call_id is not None and not settled and not lease_lost:
                try:
                    await self._ledger.settle(
                        claim,
                        call_id,
                        status="unknown"
                        if isinstance(
                            error, (TimeoutError, CallBudgetExceeded, asyncio.CancelledError)
                        )
                        else "failed",
                        usage=client.usage if client else None,
                    )
                except ReflowReviewError as failure:
                    if failure.code != "reflow_review_lease_lost":
                        raise
                    lease_lost = True
            if not lease_lost:
                # SDK/数据库异常的 code 可能为 None 或供应商自由文本，只保存业务白名单。
                code = (
                    error.code
                    if isinstance(
                        error,
                        (
                            ReflowReviewError,
                            PreparationError,
                            HumanWaitError,
                            TaskFactConflict,
                            CallBudgetExceeded,
                        ),
                    )
                    else "reflow_review_failed"
                )
                try:
                    await self._store.fail(
                        claim,
                        error_code=code,
                        retryable=not isinstance(
                            error,
                            (
                                ReflowReviewError,
                                PreparationError,
                                HumanWaitError,
                                TaskFactConflict,
                                CallBudgetExceeded,
                                asyncio.CancelledError,
                            ),
                        ),
                    )
                except ReflowReviewError as failure:
                    if failure.code != "reflow_review_lease_lost":
                        raise
            if isinstance(error, asyncio.CancelledError):
                raise
        return True

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            if not await self.run_once():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=2)
