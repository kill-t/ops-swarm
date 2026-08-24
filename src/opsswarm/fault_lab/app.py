"""order-service 故障演练 API。"""

import logging
from typing import Annotated

from fastapi import FastAPI, Path
from fastapi.responses import JSONResponse

from opsswarm import __version__
from opsswarm.fault_lab.controller import FaultController, FaultSnapshot
from opsswarm.fault_lab.models import (
    FaultErrorResponse,
    FaultStatus,
    FaultUpdate,
    HealthResponse,
    OrderResponse,
)

logger = logging.getLogger(__name__)

OrderId = Annotated[
    str,
    Path(
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="仅允许字母、数字、下划线和连字符",
    ),
]


def _to_fault_status(snapshot: FaultSnapshot) -> FaultStatus:
    return FaultStatus(enabled=snapshot.release_500_enabled)


def create_app(controller: FaultController | None = None) -> FastAPI:
    """创建可注入独立故障状态的 FastAPI 应用。"""

    fault_controller = controller if controller is not None else FaultController()
    app = FastAPI(
        title="OpsSwarm Fault Lab - order-service",
        version=__version__,
        description="仅用于本地 Docker Compose 的确定性故障演练服务。",
    )

    @app.get("/health", response_model=HealthResponse, tags=["系统"])
    def get_health() -> HealthResponse:
        return HealthResponse()

    @app.get(
        "/orders/{order_id}",
        response_model=OrderResponse,
        responses={500: {"model": FaultErrorResponse, "description": "版本故障已启用"}},
        tags=["订单"],
    )
    def get_order(order_id: OrderId) -> OrderResponse | JSONResponse:
        snapshot = fault_controller.snapshot()
        if snapshot.release_500_enabled:
            logger.warning("故障注入触发 order_id=%s fault=release_500", order_id)
            error = FaultErrorResponse(
                message="新版本故障注入已启用，订单接口模拟返回 500",
                order_id=order_id,
            )
            return JSONResponse(status_code=500, content=error.model_dump(mode="json"))

        return OrderResponse(order_id=order_id)

    @app.get(
        "/__admin/faults/release-500",
        response_model=FaultStatus,
        tags=["故障控制"],
    )
    def get_release_500() -> FaultStatus:
        return _to_fault_status(fault_controller.snapshot())

    @app.put(
        "/__admin/faults/release-500",
        response_model=FaultStatus,
        tags=["故障控制"],
    )
    def update_release_500(request: FaultUpdate) -> FaultStatus:
        snapshot = fault_controller.set_release_500(enabled=request.enabled)
        logger.info("更新故障开关 fault=release_500 enabled=%s", request.enabled)
        return _to_fault_status(snapshot)

    return app
