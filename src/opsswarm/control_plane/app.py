"""OpsSwarm 控制面 FastAPI 应用工厂。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import JsonValue

from opsswarm import __version__
from opsswarm.control_plane.routes import Clock, create_router
from opsswarm.control_plane.schemas import ApiErrorResponse
from opsswarm.database import Database, DatabaseSettings
from opsswarm.incidents import (
    IncidentDomainError,
    IncidentNotFound,
    IncidentValidationError,
    IncidentVersionConflict,
    InvalidIncidentTransition,
)
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.errors import InvestigationError, InvestigationRunNotFound
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.planning import (
    ActionPlanNotFound,
    ActionPlanNotReady,
    HypothesisNotSupported,
    PlanningError,
    UnsafeRollbackTarget,
    UnsupportedPlanningRootCause,
)
from opsswarm.planning.persistence import ActionPlanStore


def utc_now() -> datetime:
    """返回带时区的当前 UTC 时间。"""

    return datetime.now(UTC)


def create_app(
    *,
    incident_store: IncidentStore | None = None,
    investigation_store: InvestigationStore | None = None,
    action_plan_store: ActionPlanStore | None = None,
    database_settings: DatabaseSettings | None = None,
    clock: Clock = utc_now,
) -> FastAPI:
    """创建生产可管理数据库资源、测试可注入仓储的控制面应用。"""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        if any(
            store is not None for store in (incident_store, investigation_store, action_plan_store)
        ):
            if incident_store is None or investigation_store is None or action_plan_store is None:
                raise RuntimeError("注入控制面仓储时必须同时提供三个仓储")
            application.state.incident_store = incident_store
            application.state.investigation_store = investigation_store
            application.state.action_plan_store = action_plan_store
            yield
            return

        settings = database_settings if database_settings is not None else DatabaseSettings()
        database = Database(settings.database_url)
        application.state.database = database
        application.state.incident_store = IncidentStore(database.session_factory)
        application.state.investigation_store = InvestigationStore(database.session_factory)
        application.state.action_plan_store = ActionPlanStore(database.session_factory)
        try:
            yield
        finally:
            await database.dispose()

    app = FastAPI(
        title="OpsSwarm Control Plane",
        version=__version__,
        description="事故接收、调查状态与不可变时间线控制面。仅限本地 MVP 演练环境。",
        lifespan=lifespan,
    )
    app.state.incident_store = incident_store
    app.state.investigation_store = investigation_store
    app.state.action_plan_store = action_plan_store
    app.include_router(create_router(clock))

    @app.exception_handler(RequestValidationError)
    async def handle_request_validation_error(
        _: Request,
        error: RequestValidationError,
    ) -> JSONResponse:
        issues = [
            {
                "location": ".".join(str(part) for part in issue["loc"]),
                "message": str(issue["msg"]),
                "type": str(issue["type"]),
            }
            for issue in error.errors()
        ]
        response = ApiErrorResponse(
            error_code="request_validation_error",
            message="请求参数校验失败",
            details={"issues": cast(JsonValue, issues)},
        )
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content=response.model_dump(mode="json"),
        )

    @app.exception_handler(IncidentDomainError)
    async def handle_incident_domain_error(
        _: Request,
        error: IncidentDomainError,
    ) -> JSONResponse:
        if isinstance(error, IncidentNotFound):
            response = ApiErrorResponse(
                error_code="incident_not_found",
                message=str(error),
                details={"incident_id": error.incident_id},
            )
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=response.model_dump(mode="json"),
            )
        if isinstance(error, IncidentVersionConflict):
            response = ApiErrorResponse(
                error_code="incident_version_conflict",
                message=str(error),
                details={
                    "expected_version": error.expected_version,
                    "actual_version": error.actual_version,
                },
            )
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=response.model_dump(mode="json"),
            )
        if isinstance(error, InvalidIncidentTransition):
            response = ApiErrorResponse(
                error_code="invalid_incident_transition",
                message=str(error),
                details={"status": error.status, "action": error.action},
            )
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content=response.model_dump(mode="json"),
            )
        if isinstance(error, IncidentValidationError):
            response = ApiErrorResponse(
                error_code="incident_validation_error",
                message=str(error),
            )
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                content=response.model_dump(mode="json"),
            )

        response = ApiErrorResponse(error_code="incident_domain_error", message=str(error))
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=response.model_dump(mode="json"),
        )

    @app.exception_handler(InvestigationError)
    async def handle_investigation_error(
        _: Request,
        error: InvestigationError,
    ) -> JSONResponse:
        if isinstance(error, InvestigationRunNotFound):
            response = ApiErrorResponse(
                error_code="investigation_run_not_found",
                message=str(error),
                details={"run_id": str(error.run_id)},
            )
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=response.model_dump(mode="json"),
            )
        response = ApiErrorResponse(error_code="investigation_error", message=str(error))
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=response.model_dump(mode="json"),
        )

    @app.exception_handler(PlanningError)
    async def handle_planning_error(
        _: Request,
        error: PlanningError,
    ) -> JSONResponse:
        if isinstance(error, ActionPlanNotFound):
            response = ApiErrorResponse(
                error_code="action_plan_not_found",
                message=str(error),
                details={"run_id": str(error.run_id)},
            )
            return JSONResponse(
                status_code=status.HTTP_404_NOT_FOUND,
                content=response.model_dump(mode="json"),
            )
        if isinstance(error, ActionPlanNotReady):
            response = ApiErrorResponse(
                error_code="action_plan_not_ready",
                message=str(error),
                details={
                    "run_id": str(error.run_id),
                    "reason_code": error.reason_code,
                },
            )
        elif isinstance(error, HypothesisNotSupported):
            response = ApiErrorResponse(
                error_code="hypothesis_not_supported",
                message=str(error),
                details={"hypothesis_status": error.status.value},
            )
        elif isinstance(error, UnsupportedPlanningRootCause):
            response = ApiErrorResponse(
                error_code="unsupported_planning_root_cause",
                message=str(error),
                details={"root_cause": error.root_cause.value},
            )
        elif isinstance(error, UnsafeRollbackTarget):
            response = ApiErrorResponse(
                error_code="unsafe_rollback_target",
                message=str(error),
            )
        else:
            response = ApiErrorResponse(error_code="planning_error", message=str(error))
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content=response.model_dump(mode="json"),
        )

    return app
