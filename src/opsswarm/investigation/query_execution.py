"""同步只读取证的有界线程生命周期，不承担任务调度或业务写入。"""

import asyncio
import math
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from threading import Lock


class EvidenceQueryCapacityExceeded(RuntimeError):
    """仍在运行的查询占满槽位，本次没有提交底层请求。"""


class EvidenceQueryExecutorClosed(RuntimeError):
    """执行器已关闭，不能再接收查询。"""


class EvidenceQueryTimeout(TimeoutError):
    """取证等待超时，不能据此认定底层请求已经结束。"""


class ReadOnlyQueryExecutor:
    def __init__(self, *, max_queries: int = 3, timeout_seconds: float = 30) -> None:
        if type(max_queries) is not int or not 1 <= max_queries <= 3:
            raise ValueError("max_queries 必须是 1～3 的整数")
        if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 30:
            raise ValueError("timeout_seconds 必须大于 0 且不超过 30")
        self._limit = max_queries
        self._timeout = timeout_seconds
        self._lock = Lock()
        self._pending = 0
        self._closed = False
        self._pool = ThreadPoolExecutor(max_workers=max_queries, thread_name_prefix="evidence")

    async def execute[T](self, query: Callable[[], T]) -> T:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError()
        with self._lock:
            if self._closed:
                raise EvidenceQueryExecutorClosed("取证执行器已关闭")
            if self._pending >= self._limit:
                raise EvidenceQueryCapacityExceeded("取证槽位仍被未结束的查询占用")
            self._pending += 1
            try:
                future = self._pool.submit(copy_context().run, query)
            except BaseException:
                self._pending -= 1
                raise
        # 回调可能同步触发，必须放在锁外；槽位跟随底层 Future，而非等待协程。
        future.add_done_callback(self._release)
        waiting = asyncio.wrap_future(future)
        waiting.add_done_callback(_observe_completion)
        timer = asyncio.timeout(self._timeout)
        try:
            async with timer:
                return await asyncio.shield(waiting)
        except TimeoutError:
            future.cancel()
            if timer.expired():
                raise EvidenceQueryTimeout("只读取证等待超时") from None
            raise
        except asyncio.CancelledError:
            future.cancel()
            raise

    def _release[T](self, future: Future[T]) -> None:
        del future
        with self._lock:
            self._pending -= 1

    def close(self) -> None:
        """停止提交；正在运行的只读请求由原客户端超时收尾。"""

        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)


def _observe_completion[T](future: asyncio.Future[T]) -> None:
    # 超时后仍回收迟到异常，避免未读取 Future 异常；不回调业务代码。
    if not future.cancelled():
        future.exception()
