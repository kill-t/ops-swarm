"""故障开关的确定性状态控制。"""

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True, slots=True)
class FaultSnapshot:
    """某一时刻的故障开关快照。"""

    release_500_enabled: bool


class FaultController:
    """管理演练服务的进程内故障状态。"""

    def __init__(self) -> None:
        self._release_500_enabled = False
        self._lock = Lock()

    def snapshot(self) -> FaultSnapshot:
        """返回一致的只读快照，避免路由直接访问内部字段。"""

        with self._lock:
            return FaultSnapshot(release_500_enabled=self._release_500_enabled)

    def set_release_500(self, *, enabled: bool) -> FaultSnapshot:
        """显式设置版本 500 故障，并返回更新后的状态。"""

        with self._lock:
            self._release_500_enabled = enabled
            return FaultSnapshot(release_500_enabled=self._release_500_enabled)
