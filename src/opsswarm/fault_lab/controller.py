"""故障开关的确定性状态控制。"""

from dataclasses import dataclass
from threading import Lock

DEFAULT_DATABASE_POOL_SIZE = 50
DATABASE_POOL_REQUIRED_CAPACITY = 10
MIN_DATABASE_POOL_SIZE = 1
MAX_DATABASE_POOL_SIZE = 1000


@dataclass(frozen=True, slots=True)
class FaultSnapshot:
    """某一时刻的不可变演练状态快照。"""

    release_500_enabled: bool
    database_pool_size: int

    @property
    def database_pool_saturation(self) -> float:
        """根据固定演练负载计算连接池饱和度。"""

        return round(min(1.0, DATABASE_POOL_REQUIRED_CAPACITY / self.database_pool_size), 4)

    @property
    def database_pool_waiters(self) -> int:
        """返回无法立即获得连接的确定性请求数。"""

        return max(0, DATABASE_POOL_REQUIRED_CAPACITY - self.database_pool_size)

    @property
    def database_pool_saturated(self) -> bool:
        """连接池达到固定负载容量时视为饱和。"""

        return self.database_pool_saturation >= 1.0

    @property
    def database_pool_timeout_enabled(self) -> bool:
        """只有出现等待请求时才让订单稳定模拟获取连接超时。"""

        return self.database_pool_waiters > 0


class FaultController:
    """管理演练服务的进程内故障与配置状态。"""

    def __init__(self, *, database_pool_size: int = DEFAULT_DATABASE_POOL_SIZE) -> None:
        _validate_database_pool_size(database_pool_size)
        self._release_500_enabled = False
        self._database_pool_size = database_pool_size
        self._lock = Lock()

    def snapshot(self) -> FaultSnapshot:
        """返回一致的只读快照，避免路由直接访问内部字段。"""

        with self._lock:
            return self._snapshot_unlocked()

    def set_release_500(self, *, enabled: bool) -> FaultSnapshot:
        """显式设置版本 500 故障，并返回更新后的状态。"""

        with self._lock:
            self._release_500_enabled = enabled
            return self._snapshot_unlocked()

    def set_database_pool_size(
        self,
        *,
        value: int,
        expected_value: int | None = None,
    ) -> FaultSnapshot:
        """以可选的当前值条件原子设置白名单整数容量。"""

        _validate_database_pool_size(value)
        if expected_value is not None:
            _validate_database_pool_size(expected_value)
        with self._lock:
            if expected_value is not None and self._database_pool_size != expected_value:
                raise DatabasePoolValueConflict(
                    expected_value=expected_value,
                    actual_value=self._database_pool_size,
                )
            self._database_pool_size = value
            return self._snapshot_unlocked()

    def _snapshot_unlocked(self) -> FaultSnapshot:
        return FaultSnapshot(
            release_500_enabled=self._release_500_enabled,
            database_pool_size=self._database_pool_size,
        )


def _validate_database_pool_size(value: int) -> None:
    """领域入口也必须拒绝布尔值和越界容量，不能只依赖 HTTP 校验。"""

    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or not MIN_DATABASE_POOL_SIZE <= value <= MAX_DATABASE_POOL_SIZE
    ):
        raise ValueError("database_pool_size 必须是 1～1000 的整数")


class DatabasePoolValueConflict(Exception):
    """连接池配置不再等于调用方预期的当前值。"""

    def __init__(self, *, expected_value: int, actual_value: int) -> None:
        self.expected_value = expected_value
        self.actual_value = actual_value
        super().__init__(f"database_pool_size 已从预期值 {expected_value} 变为 {actual_value}")
