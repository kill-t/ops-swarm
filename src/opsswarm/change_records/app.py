"""确定性变更记录服务。"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import FastAPI, Header, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm import __version__
from opsswarm.change_records.deployment_ingestion import (
    DeploymentEvent,
    DeploymentIngestionError,
    DeploymentIngestionSettings,
    authenticate_deployment_event,
)
from opsswarm.change_records.errors import ChangeRecordIdempotencyConflict
from opsswarm.change_records.models import (
    CI_DEPLOYMENT_SOURCE,
    MAX_CHANGE_RECORDS,
    ChangeRecordListResponse,
    ChangeRecordWriteResult,
    ChangeSpec,
)
from opsswarm.change_records.persistence import ChangeRecordStore
from opsswarm.database import Database, DatabaseSettings

Clock = Callable[[], datetime]


def utc_now() -> datetime:
    """返回带时区的当前 UTC 时间。"""

    return datetime.now(UTC)


def create_app(
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    clock: Clock = utc_now,
    ingestion_settings: DeploymentIngestionSettings | None = None,
) -> FastAPI:
    """创建生产可管理数据库资源、测试可注入会话工厂的应用。"""

    owned_database: Database | None = None
    if session_factory is None:
        owned_database = Database(DatabaseSettings().database_url)
        session_factory = owned_database.session_factory
    store = ChangeRecordStore(session_factory)
    deployment_ingestion = ingestion_settings or DeploymentIngestionSettings()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned_database is not None:
                await owned_database.dispose()

    app = FastAPI(
        title="OpsSwarm Change Record Service",
        version=__version__,
        description="本地故障实验室的追加式变更事实与受限 CI/CD 部署事件服务。",
        lifespan=lifespan,
    )

    @app.exception_handler(ChangeRecordIdempotencyConflict)
    async def handle_idempotency_conflict(
        _: object,
        error: ChangeRecordIdempotencyConflict,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error_code": "change_record_idempotency_conflict",
                "message": str(error),
                "details": {"external_change_id": error.external_change_id},
            },
        )

    @app.exception_handler(DeploymentIngestionError)
    async def handle_deployment_ingestion_error(
        _: Request,
        error: DeploymentIngestionError,
    ) -> JSONResponse:
        headers = (
            {"WWW-Authenticate": "HMAC-SHA256"}
            if error.status_code == status.HTTP_401_UNAUTHORIZED
            else None
        )
        return JSONResponse(
            status_code=error.status_code,
            content={"error_code": error.error_code, "message": error.message},
            headers=headers,
        )

    @app.get("/health", tags=["系统"])
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post(
        "/__admin/changes",
        response_model=ChangeRecordWriteResult,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {
                "model": ChangeRecordWriteResult,
                "description": "相同外部变更号和内容的安全重放",
            },
            status.HTTP_409_CONFLICT: {
                "description": "外部变更号已被不同内容占用",
            },
        },
        tags=["变更模拟"],
    )
    async def record_change(
        change: ChangeSpec,
        response: Response,
    ) -> ChangeRecordWriteResult:
        """以服务端时间追加变更事实，重复内容返回原记录。"""

        current_time = clock()
        result = await store.record(
            change,
            occurred_at=current_time,
            recorded_at=current_time,
        )
        if result.deduplicated:
            response.status_code = status.HTTP_200_OK
        return result

    @app.post(
        "/api/v1/deployment-events",
        response_model=ChangeRecordWriteResult,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {
                "model": ChangeRecordWriteResult,
                "description": "相同来源、投递号、部署内容和发生时间的安全重放",
            },
            status.HTTP_400_BAD_REQUEST: {"description": "请求长度声明不合法"},
            status.HTTP_401_UNAUTHORIZED: {"description": "签名认证失败或签名已过期"},
            status.HTTP_403_FORBIDDEN: {"description": "代码或镜像仓库不在服务端白名单中"},
            status.HTTP_409_CONFLICT: {"description": "投递号已被不同部署事实占用"},
            status.HTTP_413_CONTENT_TOO_LARGE: {"description": "请求体超过 16 KiB"},
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE: {"description": "请求体不是 JSON"},
            status.HTTP_422_UNPROCESSABLE_CONTENT: {"description": "部署事件契约或时间无效"},
            status.HTTP_503_SERVICE_UNAVAILABLE: {"description": "部署事件摄取未启用"},
        },
        tags=["部署事件摄取"],
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {"application/json": {"schema": DeploymentEvent.model_json_schema()}},
            }
        },
    )
    async def ingest_deployment_event(
        request: Request,
        response: Response,
        key_id: Annotated[
            str | None,
            Header(alias="X-OpsSwarm-Key-Id"),
        ] = None,
        signed_timestamp: Annotated[
            str | None,
            Header(alias="X-OpsSwarm-Timestamp"),
        ] = None,
        signature: Annotated[
            str | None,
            Header(alias="X-OpsSwarm-Signature"),
        ] = None,
    ) -> ChangeRecordWriteResult:
        """验证 HMAC、固定仓库和时间窗口后追加部署事实。"""

        current_time = clock()
        event = await authenticate_deployment_event(
            request,
            settings=deployment_ingestion,
            current_time=current_time,
            key_id=key_id,
            timestamp=signed_timestamp,
            signature=signature,
        )
        result = await store.record(
            event.to_change(
                repository=deployment_ingestion.repository,
                image_repository=deployment_ingestion.image_repository,
            ),
            occurred_at=event.deployed_at,
            recorded_at=current_time,
            source=CI_DEPLOYMENT_SOURCE,
            source_identity=deployment_ingestion.key_id,
            source_contract_version=2,
        )
        if result.deduplicated:
            response.status_code = status.HTTP_200_OK
        return result

    @app.get(
        "/api/v1/changes",
        response_model=ChangeRecordListResponse,
        tags=["只读查询"],
    )
    async def list_changes(
        service_name: Literal["order-service"],
        lookback_hours: Annotated[int, Query(ge=1, le=24)] = 6,
    ) -> ChangeRecordListResponse:
        """在固定时间窗内读取 order-service 最近变更。"""

        generated_at = clock()
        page = await store.list_recent(
            service_name=service_name,
            since=generated_at - timedelta(hours=lookback_hours),
            until=generated_at,
            limit=MAX_CHANGE_RECORDS,
        )
        return ChangeRecordListResponse(
            generated_at=generated_at,
            items=page.items,
            truncated=page.truncated,
        )

    return app
