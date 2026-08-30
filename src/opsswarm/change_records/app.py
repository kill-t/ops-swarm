"""确定性变更记录模拟服务。"""

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal

from fastapi import FastAPI, Query, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm import __version__
from opsswarm.change_records.errors import ChangeRecordIdempotencyConflict
from opsswarm.change_records.models import (
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
) -> FastAPI:
    """创建生产可管理数据库资源、测试可注入会话工厂的应用。"""

    owned_database: Database | None = None
    if session_factory is None:
        owned_database = Database(DatabaseSettings().database_url)
        session_factory = owned_database.session_factory
    store = ChangeRecordStore(session_factory)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if owned_database is not None:
                await owned_database.dispose()

    app = FastAPI(
        title="OpsSwarm Change Simulator",
        version=__version__,
        description="仅用于本地故障实验室的追加式变更事实模拟服务。",
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
