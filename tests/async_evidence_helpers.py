"""用显式事件控制阻塞查询，避免只根据耗时猜测并发。"""

import asyncio
from threading import Event


class BlockedQuery:
    def __init__(self, *, result=(), error=None):
        self.loop = asyncio.get_running_loop()
        self.entered = asyncio.Event()
        self.release = Event()
        self.finished = Event()
        self.result = result
        self.error = error
        self.calls = 0

    def __call__(self):
        self.calls += 1
        self.loop.call_soon_threadsafe(self.entered.set)
        try:
            if not self.release.wait(5):
                raise TimeoutError("测试没有释放阻塞查询")
            if self.error is not None:
                raise self.error
            return self.result
        finally:
            self.finished.set()


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():
            await asyncio.sleep(0.005)


async def drain(executor, *queries):
    for query in queries:
        query.release.set()
    await wait_until(lambda: executor._pending == 0)
    # 让 wrap_future 和迟到异常回收回调完成。
    await asyncio.sleep(0)
    await asyncio.sleep(0)
