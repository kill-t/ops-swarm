"""故障演练服务的 HTTP 数据模型。"""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict


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
