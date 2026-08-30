"""恢复验证调度 Worker 的租约编排单元测试。"""

from datetime import UTC, datetime, timedelta
from uuid import UUID

import pytest

from opsswarm.verification import (
    VerificationWorkerSettings,
    VerificationWorkItemError,
    VerificationWorkItemStatus,
)
from opsswarm.verification.persistence import (
    StoredVerificationWorkItem,
    scheduled_sample_idempotency_key,
)
from opsswarm.verification.worker import VerificationWorker

NOW = datetime(2026, 8, 29, 12, 0, tzinfo=UTC)
WORK_ID = UUID("00000000-0000-0000-0000-000000000501")
LEASE_TOKEN = UUID("00000000-0000-0000-0000-000000000502")


def running_work_item(*, failure_count: int = 0) -> StoredVerificationWorkItem:
    return StoredVerificationWorkItem(
        id=WORK_ID,
        verification_run_id=WORK_ID,
        status=VerificationWorkItemStatus.RUNNING,
        next_sample_sequence=2,
        available_at=NOW,
        locked_by="worker-001",
        locked_until=NOW + timedelta(seconds=15),
        lease_token=LEASE_TOKEN,
        attempt_count=1,
        consecutive_failure_count=failure_count,
        last_error=None,
        created_at=NOW,
        updated_at=NOW,
        completed_at=None,
    )


class FakeStore:
    def __init__(self, item: StoredVerificationWorkItem) -> None:
        self.item = item
        self.settled = False
        self.retry_error: VerificationWorkItemError | None = None
        self.retry_delay: timedelta | None = None

    async def claim_next_work_item(self, **_: object) -> StoredVerificationWorkItem:
        return self.item

    async def settle_work_item(self, *_: object, **__: object) -> StoredVerificationWorkItem:
        self.settled = True
        return self.item

    async def retry_work_item(
        self,
        *_: object,
        error: VerificationWorkItemError,
        retry_delay: timedelta,
        **__: object,
    ) -> StoredVerificationWorkItem:
        self.retry_error = error
        self.retry_delay = retry_delay
        return self.item


class RecordingService:
    def __init__(self, *, failure: Exception | None = None) -> None:
        self.failure = failure
        self.calls: list[tuple[UUID, str]] = []

    async def sample(self, run_id: UUID, *, idempotency_key: str) -> None:
        self.calls.append((run_id, idempotency_key))
        if self.failure is not None:
            raise self.failure


@pytest.mark.asyncio
async def test_worker_uses_stable_key_and_settles_success() -> None:
    item = running_work_item()
    store = FakeStore(item)
    service = RecordingService()
    worker = VerificationWorker(
        store=store,  # type: ignore[arg-type]
        service=service,  # type: ignore[arg-type]
        worker_id="worker-001",
        clock=lambda: NOW,
    )

    assert await worker.run_once() is True
    assert service.calls == [
        (
            WORK_ID,
            scheduled_sample_idempotency_key(WORK_ID, item.next_sample_sequence),
        )
    ]
    assert store.settled is True
    assert store.retry_error is None


@pytest.mark.asyncio
async def test_worker_retries_internal_failure_with_bounded_error() -> None:
    store = FakeStore(running_work_item(failure_count=2))
    service = RecordingService(failure=RuntimeError("不应写入数据库的内部详情"))
    worker = VerificationWorker(
        store=store,  # type: ignore[arg-type]
        service=service,  # type: ignore[arg-type]
        worker_id="worker-001",
        clock=lambda: NOW,
    )

    assert await worker.run_once() is True
    assert store.settled is False
    assert store.retry_error == VerificationWorkItemError(
        error_code="verification_worker_internal_error",
        message="恢复验证调度处理失败",
    )
    assert store.retry_delay == timedelta(seconds=8)


def test_worker_settings_reject_inverted_retry_range() -> None:
    with pytest.raises(ValueError, match="最大重试延迟"):
        VerificationWorkerSettings(
            base_retry_delay_seconds=10,
            max_retry_delay_seconds=5,
        )
