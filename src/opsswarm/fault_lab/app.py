"""order-service 故障演练 API。"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import FastAPI, Path
from fastapi.responses import JSONResponse

from opsswarm import __version__
from opsswarm.fault_lab.controller import (
    DatabasePoolValueConflict,
    FaultController,
    FaultSnapshot,
    RedisAvailabilityConflict,
)
from opsswarm.fault_lab.models import (
    DatabasePoolConflictResponse,
    DatabasePoolErrorResponse,
    DatabasePoolStatus,
    DatabasePoolUpdate,
    FaultErrorResponse,
    FaultStatus,
    FaultUpdate,
    HealthResponse,
    OrderResponse,
    RedisAvailabilityConflictResponse,
    RedisDependencyErrorResponse,
    RedisDependencyStatus,
    RedisDependencyUpdate,
)
from opsswarm.fault_lab.redis_dependency import (
    LocalRedisDependencyBackend,
    RedisDependencyBackend,
    RemoteRedisDependencyBackend,
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


def _to_database_pool_status(snapshot: FaultSnapshot) -> DatabasePoolStatus:
    return DatabasePoolStatus(
        value=snapshot.database_pool_size,
        database_pool_saturation=snapshot.database_pool_saturation,
        database_pool_waiters=snapshot.database_pool_waiters,
        status="saturated" if snapshot.database_pool_saturated else "healthy",
    )


def _to_redis_dependency_status(available: bool) -> RedisDependencyStatus:
    return RedisDependencyStatus(
        available=available,
        redis_unavailable_ratio=0.0 if available else 1.0,
        status="healthy" if available else "unavailable",
    )


def create_app(
    controller: FaultController | None = None,
    *,
    redis_backend: RedisDependencyBackend | None = None,
) -> FastAPI:
    """创建故障演练应用；生产入口默认访问真实 Redis。"""

    fault_controller = controller if controller is not None else FaultController()
    dependency_backend = redis_backend
    if dependency_backend is None:
        dependency_backend = (
            LocalRedisDependencyBackend(fault_controller)
            if controller is not None
            else RemoteRedisDependencyBackend()
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await dependency_backend.close()

    app = FastAPI(
        title="OpsSwarm Fault Lab - order-service",
        version=__version__,
        description="仅用于本地 Docker Compose 的确定性故障演练服务。",
        lifespan=lifespan,
    )

    @app.get("/health", response_model=HealthResponse, tags=["系统"])
    def get_health() -> HealthResponse:
        return HealthResponse()

    @app.get(
        "/orders/{order_id}",
        response_model=OrderResponse,
        responses={
            500: {
                "model": (
                    FaultErrorResponse | DatabasePoolErrorResponse | RedisDependencyErrorResponse
                ),
                "description": "版本、数据库连接池或 Redis 依赖故障已启用",
            }
        },
        tags=["订单"],
    )
    async def get_order(order_id: OrderId) -> OrderResponse | JSONResponse:
        snapshot = fault_controller.snapshot()
        if snapshot.release_500_enabled:
            logger.warning("故障注入触发 order_id=%s fault=release_500", order_id)
            error = FaultErrorResponse(
                message="新版本故障注入已启用，订单接口模拟返回 500",
                order_id=order_id,
            )
            return JSONResponse(status_code=500, content=error.model_dump(mode="json"))

        if snapshot.database_pool_timeout_enabled:
            logger.warning(
                "故障注入触发 order_id=%s fault=database_pool_timeout pool_size=%s waiters=%s",
                order_id,
                snapshot.database_pool_size,
                snapshot.database_pool_waiters,
            )
            database_error = DatabasePoolErrorResponse(
                message="数据库连接池容量不足，订单接口模拟获取连接超时",
                order_id=order_id,
                database_pool_size=snapshot.database_pool_size,
            )
            return JSONResponse(
                status_code=500,
                content=database_error.model_dump(mode="json"),
            )

        if not await dependency_backend.is_available():
            logger.warning("故障注入触发 order_id=%s fault=redis_unavailable", order_id)
            redis_error = RedisDependencyErrorResponse(
                message="Redis 依赖不可用，订单接口模拟连接失败",
                order_id=order_id,
            )
            return JSONResponse(
                status_code=500,
                content=redis_error.model_dump(mode="json"),
            )

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

    @app.get(
        "/__admin/configuration/database-pool",
        response_model=DatabasePoolStatus,
        tags=["配置控制"],
    )
    def get_database_pool_configuration() -> DatabasePoolStatus:
        return _to_database_pool_status(fault_controller.snapshot())

    @app.put(
        "/__admin/configuration/database-pool",
        response_model=DatabasePoolStatus,
        responses={
            409: {
                "model": DatabasePoolConflictResponse,
                "description": "连接池配置当前值与调用方预期不一致",
            }
        },
        tags=["配置控制"],
    )
    def update_database_pool_configuration(
        request: DatabasePoolUpdate,
    ) -> DatabasePoolStatus | JSONResponse:
        try:
            snapshot = fault_controller.set_database_pool_size(
                value=request.value,
                expected_value=request.expected_value,
            )
        except DatabasePoolValueConflict as error:
            conflict = DatabasePoolConflictResponse(
                message=str(error),
                expected_value=error.expected_value,
                actual_value=error.actual_value,
            )
            return JSONResponse(
                status_code=409,
                content=conflict.model_dump(mode="json"),
            )
        logger.info(
            "更新演练配置 key=database_pool_size expected_value=%s value=%s saturation=%s",
            request.expected_value,
            request.value,
            snapshot.database_pool_saturation,
        )
        return _to_database_pool_status(snapshot)

    @app.get(
        "/__admin/dependencies/redis",
        response_model=RedisDependencyStatus,
        tags=["依赖控制"],
    )
    async def get_redis_dependency() -> RedisDependencyStatus:
        return _to_redis_dependency_status(await dependency_backend.is_available())

    @app.put(
        "/__admin/dependencies/redis",
        response_model=RedisDependencyStatus,
        responses={
            409: {
                "model": RedisAvailabilityConflictResponse,
                "description": "Redis 当前可用性与调用方预期不一致",
            }
        },
        tags=["依赖控制"],
    )
    async def update_redis_dependency(
        request: RedisDependencyUpdate,
    ) -> RedisDependencyStatus | JSONResponse:
        try:
            available = await dependency_backend.set_available(
                available=request.available,
                expected_available=request.expected_available,
            )
        except RedisAvailabilityConflict as error:
            conflict = RedisAvailabilityConflictResponse(
                message=str(error),
                expected_available=error.expected_available,
                actual_available=error.actual_available,
            )
            return JSONResponse(status_code=409, content=conflict.model_dump(mode="json"))
        logger.info(
            "更新演练依赖 dependency=redis expected_available=%s available=%s",
            request.expected_available,
            request.available,
        )
        return _to_redis_dependency_status(available)

    return app
