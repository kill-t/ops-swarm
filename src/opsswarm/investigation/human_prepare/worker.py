"""独立 Worker 消费人工材料，产出可复核输入而非模型结论。"""

import asyncio
from collections.abc import Callable
from contextlib import suppress

from opsswarm.investigation.human_prepare.builder import build_prepared_input
from opsswarm.investigation.human_prepare.contracts import (
    PreparationError,
    PreparationSource,
    PreparedInput,
)
from opsswarm.investigation.human_prepare.job_store import HumanPreparationJobStore
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.tasks.errors import TaskFactConflict


class HumanPreparationWorker:
    def __init__(
        self,
        store: HumanPreparationJobStore,
        *,
        worker_id: str,
        builder: Callable[[PreparationSource], PreparedInput] = build_prepared_input,
    ) -> None:
        self._store, self._worker_id, self._builder = store, worker_id, builder

    async def run_once(self) -> bool:
        claim = await self._store.claim_next(worker_id=self._worker_id)
        if claim is None:
            return False
        try:
            source = await self._store.get_source(claim)
            prepared = self._builder(source)
            await self._store.complete(claim, prepared)
        except Exception as error:
            if isinstance(error, PreparationError) and error.code == "human_preparation_lease_lost":
                return True
            try:
                await self._store.fail(
                    claim,
                    error_code=(
                        error.code
                        if isinstance(error, PreparationError)
                        else "human_preparation_source_conflict"
                        if isinstance(error, (HumanWaitError, TaskFactConflict))
                        else "human_preparation_failed"
                    ),
                    retryable=not isinstance(
                        error, (PreparationError, HumanWaitError, TaskFactConflict)
                    ),
                )
            except PreparationError as failure:
                if failure.code != "human_preparation_lease_lost":
                    raise
        return True

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            if not await self.run_once():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=2)
