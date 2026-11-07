"""独立进程执行有限只读采样，每份业务样本立即持久化。"""

import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress

from opsswarm.verification.candidate.contracts import RecoveryClaim, RecoveryError
from opsswarm.verification.candidate.policy import (
    MIN_SAMPLE_INTERVAL_SECONDS,
    RECOVERY_LEASE_SECONDS,
)
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
        deadline = asyncio.get_running_loop().time() + RECOVERY_LEASE_SECONDS
        try:
            for index in range(3):
                if index:
                    await self._sleep(MIN_SAMPLE_INTERVAL_SECONDS)
                sequence = await self._authorize_sample(claim, deadline)
                if sequence is None:
                    return True
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

    async def _authorize_sample(self, claim: RecoveryClaim, deadline: float) -> int | None:
        # UTC 可能被校正；每次仍由仓储核验原租约，单调截止不随等待重置。
        loop = asyncio.get_running_loop()
        timer = asyncio.timeout_at(deadline)
        try:
            async with timer:
                while loop.time() < deadline:
                    try:
                        return await self._store.authorize_sample(claim)
                    except RecoveryError as error:
                        if error.code != "recovery_sample_too_early":
                            raise
                    await self._sleep(MIN_SAMPLE_INTERVAL_SECONDS)
        except TimeoutError:
            if not timer.expired():
                raise
        return None

    async def run_forever(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            if not await self.run_once():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=2)
