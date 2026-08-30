"""故障演练服务的 HTTP 数据模型。"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """拒绝未声明字段，避免演练控制参数被静默忽略。"""

    model_config = ConfigDict(extra="forbid")


class HealthResponse(StrictModel):
    """健康检查响应。"""

    service: Literal["order-service"] = "order-service"
    status: Literal["healthy"] = "healthy"


class FaultUpdate(StrictModel):
    """故障开关更新请求。"""

    enabled: bool


class FaultStatus(StrictModel):
    """故障开关当前状态。"""

    name: Literal["release_500"] = "release_500"
    enabled: bool


class DatabasePoolUpdate(StrictModel):
    """数据库连接池容量更新请求。"""

    value: int = Field(strict=True, ge=1, le=1000)
    expected_value: int | None = Field(default=None, strict=True, ge=1, le=1000)


class DatabasePoolStatus(StrictModel):
    """数据库连接池配置及确定性负载状态。"""

    key: Literal["database_pool_size"] = "database_pool_size"
    value: int
    database_pool_saturation: float = Field(ge=0, le=1)
    database_pool_waiters: int = Field(ge=0)
    status: Literal["healthy", "saturated"]


class OrderResponse(StrictModel):
    """演练订单查询结果。"""

    order_id: str
    status: Literal["confirmed"] = "confirmed"
    amount: Decimal = Decimal("99.00")
    currency: Literal["CNY"] = "CNY"


class FaultErrorResponse(StrictModel):
    """故障注入后的结构化错误。"""

    error_code: Literal["release_500"] = "release_500"
    message: str
    order_id: str


class DatabasePoolErrorResponse(StrictModel):
    """连接池容量不足时的结构化订单错误。"""

    error_code: Literal["database_pool_timeout"] = "database_pool_timeout"
    message: str
    order_id: str
    database_pool_size: int


class DatabasePoolConflictResponse(StrictModel):
    """比较并设置发现配置已经漂移。"""

    error_code: Literal["database_pool_value_conflict"] = "database_pool_value_conflict"
    message: str
    expected_value: int
    actual_value: int


class RedisDependencyUpdate(StrictModel):
    """Redis 依赖可用性比较并设置请求。"""

    available: bool = Field(strict=True)
    expected_available: bool | None = Field(default=None, strict=True)


class RedisDependencyStatus(StrictModel):
    """Redis 依赖的确定性演练状态。"""

    dependency: Literal["redis"] = "redis"
    available: bool
    redis_unavailable_ratio: float = Field(ge=0, le=1)
    status: Literal["healthy", "unavailable"]


class RedisDependencyErrorResponse(StrictModel):
    """Redis 不可用时的结构化订单错误。"""

    error_code: Literal["redis_connection_unavailable"] = "redis_connection_unavailable"
    message: str
    order_id: str
    dependency: Literal["redis"] = "redis"


class RedisAvailabilityConflictResponse(StrictModel):
    """比较并设置发现 Redis 可用性已经改变。"""

    error_code: Literal["redis_availability_conflict"] = "redis_availability_conflict"
    message: str
    expected_available: bool
    actual_available: bool
