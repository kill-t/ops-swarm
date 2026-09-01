"""order-service 故障演练 API。"""

import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import FastAPI, Path
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST

from opsswarm import __version__
from opsswarm.fault_lab.controller import (
    DatabasePoolValueConflict,
    FaultController,
    FaultSnapshot,
    RedisAvailabilityConflict,
)
from opsswarm.fault_lab.metrics import FaultLabMetrics
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
from opsswarm.fault_lab.runtime_metadata import (
    RuntimeMetadataResponse,
    RuntimeMetadataSettings,
    RuntimeMetadataUnavailableResponse,
)
from opsswarm.fault_lab.structured_logging import FaultKind, FaultLabEventLogger

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(UTC)


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
    event_logger: FaultLabEventLogger | None = None,
    runtime_metadata_settings: RuntimeMetadataSettings | None = None,
    runtime_clock: Callable[[], datetime] = _utc_now,
) -> FastAPI:
    """创建故障演练应用；生产入口默认访问真实 Redis。"""

    fault_controller = controller if controller is not None else FaultController()
    fault_metrics = FaultLabMetrics()
    fault_event_logger = event_logger if event_logger is not None else FaultLabEventLogger()
    runtime_settings = (
        runtime_metadata_settings
        if runtime_metadata_settings is not None
        else RuntimeMetadataSettings()
    )
    runtime_identity = runtime_settings.identity()
    runtime_started_at = runtime_clock()
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
            fault_event_logger.close()

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
        "/api/v1/runtime-metadata",
        response_model=RuntimeMetadataResponse,
        responses={
            503: {
                "model": RuntimeMetadataUnavailableResponse,
                "description": "部署平台没有为当前运行进程注入完整镜像身份",
            }
        },
        tags=["运行元数据"],
    )
    def get_runtime_metadata() -> RuntimeMetadataResponse | JSONResponse:
        """只报告启动时注入的身份，不读取 Docker Socket 或主机文件。"""

        if runtime_identity is None:
            unavailable = RuntimeMetadataUnavailableResponse()
            return JSONResponse(
                status_code=503,
                content=unavailable.model_dump(mode="json"),
            )
        return RuntimeMetadataResponse(
            **runtime_identity.model_dump(),
            started_at=runtime_started_at,
            reported_at=runtime_clock(),
        )

    @app.get("/metrics", include_in_schema=False)
    async def get_metrics() -> Response:
        payload = fault_metrics.render(
            snapshot=fault_controller.snapshot(),
            redis_available=await dependency_backend.is_available(),
        )
        return Response(content=payload, media_type=CONTENT_TYPE_LATEST)

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
            fault_metrics.record_order(result="failed", failure_reason="release_500")
            fault_event_logger.order_failure(fault=FaultKind.RELEASE_500, snapshot=snapshot)
            error = FaultErrorResponse(
                message="新版本故障注入已启用，订单接口模拟返回 500",
                order_id=order_id,
            )
            return JSONResponse(status_code=500, content=error.model_dump(mode="json"))

        if snapshot.database_pool_timeout_enabled:
            fault_metrics.record_order(result="failed", failure_reason="database_pool_timeout")
            fault_event_logger.order_failure(
                fault=FaultKind.DATABASE_POOL_TIMEOUT,
                snapshot=snapshot,
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
            fault_metrics.record_order(result="failed", failure_reason="redis_unavailable")
            fault_event_logger.order_failure(fault=FaultKind.REDIS_UNAVAILABLE, snapshot=snapshot)
            redis_error = RedisDependencyErrorResponse(
                message="Redis 依赖不可用，订单接口模拟连接失败",
                order_id=order_id,
            )
            return JSONResponse(
                status_code=500,
                content=redis_error.model_dump(mode="json"),
            )

        fault_metrics.record_order(result="success", failure_reason="none")
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
