"""在已提交预留之外运行只读取证和一次结构化专业分析。"""

import asyncio
from collections.abc import Callable
from contextlib import suppress
from uuid import UUID

from opsswarm.investigation.budget.model_contracts import ModelBudgetPolicy
from opsswarm.investigation.budget.policy import CallBudgetExceeded
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_execution.contracts import ANALYZE, COLLECT
from opsswarm.investigation.reinvestigation_execution.evidence import ExecutionEvidenceSource
from opsswarm.investigation.reinvestigation_execution.ledger import ExecutionLedger
from opsswarm.investigation.reinvestigation_execution.model import ExecutionModel
from opsswarm.investigation.reinvestigation_execution.store import ExecutionStore


class ExecutionWorker:
    def __init__(
        self,
        store: ExecutionStore,
        ledger: ExecutionLedger,
        evidence: ExecutionEvidenceSource,
        *,
        model_factory: Callable[[ModelBudgetPolicy], ExecutionModel],
        worker_id: str,
    ):
        self._store, self._ledger, self._evidence = store, ledger, evidence
        self._factory, self._worker_id = model_factory, worker_id

    async def run_once(self) -> bool:
        prepared = await self._store.prepare_next()
        claim = await self._store.claim_next(worker_id=self._worker_id)
        if claim is None:
            return prepared
        call_id: UUID | None = None
        settled = False
        client = None
        model_phase = False
        try:
            source, window = await self._store.context(claim)
            client = self._factory(source.source.model_budget)
            grant = await self._ledger.reserve(claim, COLLECT)
            call_id, settled = grant.record.id, not grant.created
            if grant.created:
                async with asyncio.timeout(min(30, await self._ledger.check(claim))):
                    snapshot = await self._evidence.collect(
                        source.assignment, claim.task_id, window
                    )
                await self._ledger.save_evidence(claim, call_id, snapshot)
                settled = True
            packet = await self._ledger.prepare_packet(claim)
            grant = await self._ledger.reserve(claim, ANALYZE)
            call_id, settled, model_phase = grant.record.id, not grant.created, True
            if grant.created:
                async with asyncio.timeout(await self._ledger.check(claim)):
                    result = await client.invoke(packet.model_input)
                accepted = await self._ledger.settle(
                    claim, call_id, status="succeeded", result=result, usage=client.usage
                )
                settled = True
                if not accepted:
                    raise ReflowError(
                        "execution_result_rejected", "来源、期限或用量变化后不发布专业报告"
                    )
            await self._store.complete(claim, call_id)
        except (Exception, asyncio.CancelledError) as error:
            lost = isinstance(error, ReflowError) and error.code == "execution_lease_lost"
            if call_id is not None and not settled and not lost:
                try:
                    await self._ledger.settle(
                        claim,
                        call_id,
                        status="unknown"
                        if isinstance(
                            error, (TimeoutError, asyncio.CancelledError, CallBudgetExceeded)
                        )
                        else "failed",
                        usage=client.usage if client and model_phase else None,
                    )
                except ReflowError as failure:
                    if failure.code != "execution_lease_lost":
                        raise
                    lost = True
            if not lost:
                code = (
                    error.code
                    if isinstance(error, (ReflowError, CallBudgetExceeded))
                    else "execution_timeout"
                    if isinstance(error, TimeoutError)
                    else "execution_failed"
                )
                try:
                    await self._store.fail(
                        claim,
                        error_code=code,
                        retryable=not isinstance(error, (ReflowError, CallBudgetExceeded)),
                    )
                except ReflowError as failure:
                    if failure.code != "execution_lease_lost":
                        raise
            if isinstance(error, asyncio.CancelledError):
                raise
        return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not await self.run_once():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=2)
