"""逐次恢复采样的应用编排。"""

from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from opsswarm.verification.adapter import VerificationAdapter
from opsswarm.verification.errors import VerificationProbeFailure
from opsswarm.verification.models import VerificationFailure
from opsswarm.verification.persistence import (
    StoredVerificationRun,
    StoredVerificationWorkItem,
    VerificationCandidatePage,
    VerificationRunCreation,
    VerificationSampleOutcome,
    VerificationStore,
)

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """返回带时区的当前 UTC 时间。"""

    return datetime.now(UTC)


class VerificationService:
    """创建持久运行并用固定只读探针追加样本。"""

    def __init__(
        self,
        store: VerificationStore,
        adapter: VerificationAdapter,
        *,
        clock: Clock = utc_now,
    ) -> None:
        self._store = store
        self._adapter = adapter
        self._clock = clock

    async def create_run(
        self,
        execution_id: UUID,
        *,
        expected_incident_version: int,
    ) -> VerificationRunCreation:
        return await self._store.create_for_execution(
            execution_id,
            expected_incident_version=expected_incident_version,
            occurred_at=self._clock(),
        )

    async def list_candidates(self, *, limit: int = 50) -> VerificationCandidatePage:
        """返回当前事故周期内成功但尚未完成验证的操作。"""

        return await self._store.list_candidates(limit=limit)

    async def sample(
        self,
        run_id: UUID,
        *,
        idempotency_key: str,
    ) -> VerificationSampleOutcome:
        prepared = await self._store.prepare_sample(
            run_id,
            idempotency_key=idempotency_key,
        )
        if prepared.existing_sample is not None:
            return VerificationSampleOutcome(
                run=prepared.run,
                sample=prepared.existing_sample,
                created=False,
                probe_performed=False,
            )
        observation = None
        failure = None
        try:
            observation = await self._adapter.observe(prepared.step)
        except VerificationProbeFailure as error:
            failure = VerificationFailure(
                error_code=error.error_code,
                message=str(error),
                http_status=error.http_status,
            )
        except Exception:
            failure = VerificationFailure(
                error_code="verification_internal_error",
                message="恢复验证探针发生未分类错误",
            )
        run, sample, created = await self._store.record_sample(
            run_id,
            idempotency_key=idempotency_key,
            observed_at=self._clock(),
            observation=observation,
            failure=failure,
        )
        return VerificationSampleOutcome(
            run=run,
            sample=sample,
            created=created,
            probe_performed=True,
        )

    async def get(self, run_id: UUID) -> StoredVerificationRun:
        return await self._store.get(run_id)

    async def get_work_item(self, run_id: UUID) -> StoredVerificationWorkItem:
        """返回调度状态，供控制面只读观测下一次采样和租约。"""

        return await self._store.get_work_item(run_id)
