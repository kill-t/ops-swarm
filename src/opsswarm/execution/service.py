"""持久租约与外部白名单适配器之间的应用编排。"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from opsswarm.execution.adapter import OperationAdapter
from opsswarm.execution.errors import OperationAdapterFailure
from opsswarm.execution.models import EXECUTION_LEASE_DURATION, OperationFailure
from opsswarm.execution.persistence import (
    ExecutionCandidatePage,
    OperationExecutionOutcome,
    OperationExecutionStore,
    StoredOperationExecution,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """返回执行完成时使用的 UTC 时间。"""

    return datetime.now(UTC)


class OperationExecutionService:
    """执行一个短时本地动作，并把所有结果交给持久仓储。"""

    def __init__(
        self,
        store: OperationExecutionStore,
        adapter: OperationAdapter,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._clock = clock

    async def list_candidates(self, *, limit: int = 50) -> ExecutionCandidatePage:
        """读取可以交由当前确定性执行器处理的计划。"""

        return await self._store.list_candidates(limit=limit)

    async def execute_step(
        self,
        step_id: UUID,
        *,
        idempotency_key: str,
        expected_incident_version: int,
        occurred_at: datetime,
    ) -> OperationExecutionOutcome:
        claim = await self._store.claim(
            step_id,
            idempotency_key=idempotency_key,
            expected_incident_version=expected_incident_version,
            occurred_at=occurred_at,
            lease_duration=EXECUTION_LEASE_DURATION,
        )
        if not claim.claimed:
            return OperationExecutionOutcome(
                execution=claim.execution,
                created=False,
                execution_performed=False,
            )
        if claim.execution.lease_token is None:
            raise RuntimeError("已领取执行缺少租约令牌")
        try:
            result = await self._adapter.execute(claim.step)
        except OperationAdapterFailure as error:
            completed = await self._store.complete(
                claim.execution.id,
                lease_token=claim.execution.lease_token,
                occurred_at=self._clock(),
                failure=OperationFailure(
                    error_code=error.error_code,
                    message=str(error),
                ),
            )
        except Exception:
            completed = await self._store.complete(
                claim.execution.id,
                lease_token=claim.execution.lease_token,
                occurred_at=self._clock(),
                failure=OperationFailure(
                    error_code="executor_internal_error",
                    message="白名单执行器发生未分类错误",
                ),
            )
        else:
            completed = await self._store.complete(
                claim.execution.id,
                lease_token=claim.execution.lease_token,
                occurred_at=self._clock(),
                result=result,
            )
        return OperationExecutionOutcome(
            execution=completed,
            created=claim.created,
            execution_performed=True,
        )

    async def get(self, execution_id: UUID) -> StoredOperationExecution:
        return await self._store.get(execution_id)
