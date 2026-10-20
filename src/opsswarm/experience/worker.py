"""独立经验 Worker 只投影已冻结事实，不持有模型或环境写工具。"""

import asyncio
from collections.abc import Callable
from contextlib import suppress

from opsswarm.experience.contracts import ExperienceDraft, ExperienceError, ExperienceSource
from opsswarm.experience.projection import project_source
from opsswarm.experience.store import ExperienceStore


class ExperienceWorker:
    def __init__(
        self,
        store: ExperienceStore,
        *,
        worker_id: str,
        projector: Callable[[ExperienceSource], ExperienceDraft] = project_source,
    ) -> None:
        self._store = store
        self._worker_id = worker_id
        self._projector = projector

    async def run_once(self) -> bool:
        claim = await self._store.claim_next(worker_id=self._worker_id)
        if claim is None:
            return False
        try:
            source = await self._store.get_claim_source(claim)
            draft = self._projector(source)
            await self._store.complete(claim, draft)
        except Exception as error:
            if isinstance(error, ExperienceError) and error.code == "experience_lease_lost":
                return True
            code = (
                error.code if isinstance(error, ExperienceError) else "experience_generation_failed"
            )
            try:
                await self._store.fail(
                    claim, error_code=code, retryable=not isinstance(error, ExperienceError)
                )
            except ExperienceError as failure:
                if failure.code != "experience_lease_lost":
                    raise
        return True

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            if not await self.run_once():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=2)
