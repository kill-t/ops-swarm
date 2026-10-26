"""独立进程执行有限只读采样，每份业务样本立即持久化。"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from opsswarm.verification.candidate.contracts import RecoveryError
from opsswarm.verification.candidate.probe import Probe, ProbeFailure
from opsswarm.verification.candidate.store import CandidateRecoveryStore


class CandidateRecoveryWorker:
    def __init__(
        self,
        store: CandidateRecoveryStore,
        *,
        probe: Probe,
        worker_id: str,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._store, self._probe, self._worker_id, self._sleep = store, probe, worker_id, sleep

    async def run_once(self) -> bool:
        claim = await self._store.claim_next(worker_id=self._worker_id)
        if claim is None:
            return False
        try:
            for index in range(3):
                if index:
                    await self._sleep(2)
                sequence = await self._store.authorize_sample(claim)
                try:
                    observation = await self._probe.observe()
                except ProbeFailure as error:
                    await self._store.save_sample(claim, sequence, error_code=error.code)
                    break
                await self._store.save_sample(claim, sequence, observation=observation)
            await self._store.complete(claim)
        except RecoveryError as error:
            if error.code != "recovery_lease_lost":
                raise
            # 已持久样本仍属原尝试；后续扫描接管，不让迟到结果推进流程。
        return True

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not await self.run_once():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=2)
