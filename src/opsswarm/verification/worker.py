"""使用 PostgreSQL 租约驱动恢复验证自动采样。"""

import asyncio
import logging
from collections.abc import Callable
from contextlib import suppress
from datetime import datetime, timedelta

from opsswarm.verification import (
    VerificationRunCompleted,
    VerificationWorkItemError,
)
from opsswarm.verification.persistence import (
    VerificationStore,
    scheduled_sample_idempotency_key,
)
from opsswarm.verification.service import VerificationService

logger = logging.getLogger(__name__)


class VerificationWorker:
    """领取一个持久任务，执行幂等采样并安排下一次到期时间。"""

    def __init__(
        self,
        *,
        store: VerificationStore,
        service: VerificationService,
        worker_id: str,
        clock: Callable[[], datetime],
        lease_duration: timedelta = timedelta(seconds=15),
        base_retry_delay: timedelta = timedelta(seconds=2),
        max_retry_delay: timedelta = timedelta(minutes=1),
    ) -> None:
        if base_retry_delay < timedelta(0):
            raise ValueError("base_retry_delay 不能小于 0")
        if max_retry_delay < base_retry_delay:
            raise ValueError("max_retry_delay 不能小于 base_retry_delay")
        self._store = store
        self._service = service
        self._worker_id = worker_id
        self._clock = clock
        self._lease_duration = lease_duration
        self._base_retry_delay = base_retry_delay
        self._max_retry_delay = max_retry_delay

    async def run_once(self) -> bool:
        """领取至多一个到期任务；当前无任务时返回 False。"""

        claimed = await self._store.claim_next_work_item(
            worker_id=self._worker_id,
            occurred_at=self._clock(),
            lease_duration=self._lease_duration,
        )
        if claimed is None:
            return False
        logger.info(
            "领取恢复验证任务 work_item_id=%s run_id=%s sample_sequence=%s attempt=%s",
            claimed.id,
            claimed.verification_run_id,
            claimed.next_sample_sequence,
            claimed.attempt_count,
        )
        lease_token = claimed.lease_token
        if lease_token is None:
            raise RuntimeError("已领取验证任务缺少 lease_token")
        idempotency_key = scheduled_sample_idempotency_key(
            claimed.id,
            claimed.next_sample_sequence,
        )
        try:
            await self._service.sample(
                claimed.verification_run_id,
                idempotency_key=idempotency_key,
            )
        except VerificationRunCompleted:
            # 人工采样可能先一步结束运行；任务只需收敛为 completed。
            pass
        except Exception as error:
            work_error = _safe_error(error)
            await self._store.retry_work_item(
                claimed.id,
                lease_token=lease_token,
                occurred_at=self._clock(),
                error=work_error,
                retry_delay=self._retry_delay(claimed.consecutive_failure_count),
            )
            logger.warning(
                "恢复验证任务进入退避 work_item_id=%s run_id=%s error_code=%s",
                claimed.id,
                claimed.verification_run_id,
                work_error.error_code,
            )
            return True

        await self._store.settle_work_item(
            claimed.id,
            lease_token=lease_token,
            occurred_at=self._clock(),
        )
        logger.info(
            "结算恢复验证任务 work_item_id=%s run_id=%s sample_sequence=%s",
            claimed.id,
            claimed.verification_run_id,
            claimed.next_sample_sequence,
        )
        return True

    async def run_forever(
        self,
        *,
        poll_interval: timedelta,
        stop_event: asyncio.Event | None = None,
    ) -> None:
        """持续轮询到期任务；停止信号不改变任何持久业务状态。"""

        if not timedelta(milliseconds=100) <= poll_interval <= timedelta(minutes=1):
            raise ValueError("poll_interval 必须在 100 毫秒到 1 分钟之间")
        while stop_event is None or not stop_event.is_set():
            processed = await self.run_once()
            if processed:
                continue
            if stop_event is None:
                await asyncio.sleep(poll_interval.total_seconds())
                continue
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    stop_event.wait(),
                    timeout=poll_interval.total_seconds(),
                )

    def _retry_delay(self, consecutive_failure_count: int) -> timedelta:
        multiplier = 2 ** min(consecutive_failure_count, 20)
        calculated = timedelta(seconds=self._base_retry_delay.total_seconds() * multiplier)
        return calculated if calculated <= self._max_retry_delay else self._max_retry_delay


def _safe_error(error: Exception) -> VerificationWorkItemError:
    """调度错误只保存稳定分类，不持久化 URL、响应体或异常堆栈。"""

    if isinstance(error, TimeoutError):
        return VerificationWorkItemError(
            error_code="verification_worker_timeout",
            message="恢复验证调度处理超时",
        )
    return VerificationWorkItemError(
        error_code="verification_worker_internal_error",
        message="恢复验证调度处理失败",
    )
