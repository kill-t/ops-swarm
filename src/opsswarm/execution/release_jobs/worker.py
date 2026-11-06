"""单次写出与仅GET恢复的独立发布执行Worker。"""

import asyncio
from contextlib import suppress

from opsswarm.execution.release_jobs.client import DispatchClientError, ReleaseDispatchClient
from opsswarm.execution.release_jobs.contracts import ExecutionJobError
from opsswarm.execution.release_jobs.job_store import ReleaseExecutionJobStore
from opsswarm.identity.models import OperatorAccessError


class ReleaseExecutionWorker:
    def __init__(
        self, store: ReleaseExecutionJobStore, *, client: ReleaseDispatchClient, worker_id: str
    ) -> None:
        self._store, self._client, self._worker_id = store, client, worker_id

    async def run_once(self) -> bool:
        claim = await self._store.claim_next(worker_id=self._worker_id)
        if claim is None:
            return False
        try:
            # 领取时已在父锁下核对阶段，实际派发仍由新事务复核全部来源。
            if claim.phase == "dispatched":
                command, _ = await self._store.get_dispatch(claim)
                observation = await self._client.lookup(command)
            else:
                command = await self._store.begin_dispatch(claim)
                await self._client.register(command)
                command = await self._store.authorize_execute(claim)
                observation = await self._client.execute(command)
            await self._store.complete(
                claim, observation, reason_code="release_execution_remote_observed"
            )
        except Exception as error:
            if (
                isinstance(error, ExecutionJobError)
                and error.code == "release_execution_lease_lost"
            ):
                return True
            # 恢复原操作前仍核验当前租约与完整历史，损坏不能被降级为普通失败。
            try:
                command, dispatched = await self._store.get_dispatch(claim)
            except ExecutionJobError as failure:
                if failure.code == "release_execution_lease_lost":
                    return True
                raise
            observation = None
            if dispatched:
                with suppress(DispatchClientError):
                    observation = await self._client.lookup(command)
            code = (
                error.code
                if isinstance(error, (ExecutionJobError, OperatorAccessError, DispatchClientError))
                else "release_execution_failed"
            )
            try:
                await self._store.complete(claim, observation, reason_code=code)
            except ExecutionJobError as failure:
                if failure.code != "release_execution_lease_lost":
                    raise
        return True

    async def run_forever(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            if not await self.run_once():
                with suppress(TimeoutError):
                    await asyncio.wait_for(stop_event.wait(), timeout=2)
