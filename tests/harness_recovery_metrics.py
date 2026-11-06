"""为真实持久化恢复用例采集分阶段墙钟耗时。"""

from collections.abc import Iterator
from contextlib import contextmanager
from functools import wraps
from time import perf_counter_ns

from pytest import MonkeyPatch


class PhaseMeasurements:
    def __init__(self) -> None:
        self.durations_ms: dict[str, list[float]] = {}

    @contextmanager
    def measure(self, phase: str) -> Iterator[None]:
        started = perf_counter_ns()
        try:
            yield
        finally:
            elapsed = (perf_counter_ns() - started) / 1_000_000
            self.durations_ms.setdefault(phase, []).append(round(elapsed, 3))

    def instrument(self, patch: MonkeyPatch, owner: type, name: str, phase: str) -> None:
        method = getattr(owner, name)

        @wraps(method)
        async def timed(*args, **kwargs):
            with self.measure(phase):
                return await method(*args, **kwargs)

        patch.setattr(owner, name, timed)
