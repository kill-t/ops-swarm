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
from opsswarm.approval import (
    ActionPlanDoesNotRequireApproval,
    ActionPlanNotCurrent,
    ActionPlanNotDecidable,
    ActionPlanNotSubmittable,
    ApprovalActionPlanNotFound,
    ApprovalDeciderMustBeHuman,
    ApprovalError,
    ApprovalPlanIntegrityViolation,
    ApprovalRequestAlreadyDecided,
    ApprovalRequesterMustBeHuman,
    ApprovalRequestExpired,
    ApprovalRequestNotFound,
)
from opsswarm.approval.persistence import ApprovalRequestStore
from opsswarm.control_plane.routes import Clock, create_router
from opsswarm.control_plane.schemas import ApiErrorResponse
from opsswarm.database import Database, DatabaseSettings
from opsswarm.evaluation.errors import EvaluationRunNotFound
from opsswarm.evaluation.persistence import EvaluationStore
from opsswarm.execution import (
    ExecutionError,
    ExecutionLeaseLost,
    FaultLabOperationAdapter,
    OperationAdapter,
    OperationExecutionFailed,
    OperationExecutionNotFound,
    OperationIdempotencyConflict,
    OperationNotAuthorized,
    OperationStepAlreadyRunning,
    OperationStepAlreadySucceeded,
    OperationStepNotFound,
)
from opsswarm.execution.persistence import OperationExecutionStore
from opsswarm.execution.service import OperationExecutionService
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
from opsswarm.verification import (
    FaultLabVerificationAdapter,
    OperationExecutionNotVerifiable,
    UnsupportedVerificationRule,
    VerificationAdapter,
    VerificationError,
    VerificationRunCompleted,
    VerificationRunNotFound,
)
from opsswarm.verification.persistence import VerificationStore
from opsswarm.verification.service import VerificationService
from opsswarm.workroom import WorkroomStore


def utc_now() -> datetime:
    """返回带时区的当前 UTC 时间。"""

    return datetime.now(UTC)


def create_app(
    *,
    incident_store: IncidentStore | None = None,
    investigation_store: InvestigationStore | None = None,
    action_plan_store: ActionPlanStore | None = None,
    approval_request_store: ApprovalRequestStore | None = None,
    operation_execution_store: OperationExecutionStore | None = None,
    operation_adapter: OperationAdapter | None = None,
    verification_store: VerificationStore | None = None,
    verification_adapter: VerificationAdapter | None = None,
    workroom_store: WorkroomStore | None = None,
    evaluation_store: EvaluationStore | None = None,
    database_settings: DatabaseSettings | None = None,
    clock: Clock = utc_now,
) -> FastAPI:
    """创建生产可管理数据库资源、测试可注入仓储的控制面应用。"""

    @asynccontextmanager
    async def lifespan(application: FastAPI) -> AsyncIterator[None]:
        stores = (
            incident_store,
            investigation_store,
            action_plan_store,
            approval_request_store,
            operation_execution_store,
            verification_store,
            workroom_store,
            evaluation_store,
        )
        if any(store is not None for store in stores):
            if any(store is None for store in stores):
                raise RuntimeError("注入控制面仓储时必须同时提供八个仓储")
            application.state.incident_store = incident_store
            application.state.investigation_store = investigation_store
            application.state.action_plan_store = action_plan_store
            application.state.approval_request_store = approval_request_store
            application.state.operation_execution_service = OperationExecutionService(
                cast(OperationExecutionStore, operation_execution_store),
                operation_adapter if operation_adapter is not None else FaultLabOperationAdapter(),
                clock=clock,
            )
            application.state.verification_service = VerificationService(
                cast(VerificationStore, verification_store),
                (
                    verification_adapter
                    if verification_adapter is not None
                    else FaultLabVerificationAdapter()
                ),
                clock=clock,
            )
            application.state.workroom_store = workroom_store
            application.state.evaluation_store = evaluation_store
            yield
            return

        settings = database_settings if database_settings is not None else DatabaseSettings()
        database = Database(settings.database_url)
        application.state.database = database
        application.state.incident_store = IncidentStore(database.session_factory)
        application.state.investigation_store = InvestigationStore(database.session_factory)
        application.state.action_plan_store = ActionPlanStore(database.session_factory)
        application.state.approval_request_store = ApprovalRequestStore(database.session_factory)
        execution_store = OperationExecutionStore(database.session_factory)
        application.state.operation_execution_service = OperationExecutionService(
            execution_store,
            operation_adapter if operation_adapter is not None else FaultLabOperationAdapter(),
            clock=clock,
        )
        application.state.verification_service = VerificationService(
            VerificationStore(database.session_factory),
            (
                verification_adapter
                if verification_adapter is not None
                else FaultLabVerificationAdapter()
            ),
            clock=clock,
        )
        application.state.workroom_store = WorkroomStore(database.session_factory)
        application.state.evaluation_store = EvaluationStore(database.session_factory)
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
    app.state.approval_request_store = approval_request_store
    app.state.operation_execution_service = None
    app.state.verification_service = None
    app.state.workroom_store = workroom_store
    app.state.evaluation_store = evaluation_store
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

    @app.exception_handler(EvaluationRunNotFound)
    async def handle_evaluation_run_not_found(
        _: Request,
        error: EvaluationRunNotFound,
    ) -> JSONResponse:
        response = ApiErrorResponse(
            error_code="evaluation_run_not_found",
            message=str(error),
            details={"run_id": str(error.run_id)},
        )
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
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

    @app.exception_handler(ApprovalError)
    async def handle_approval_error(
        _: Request,
        error: ApprovalError,
    ) -> JSONResponse:
        http_status = status.HTTP_409_CONFLICT
        if isinstance(error, ApprovalActionPlanNotFound):
            response = ApiErrorResponse(
                error_code="approval_action_plan_not_found",
                message=str(error),
                details={"plan_id": str(error.plan_id)},
            )
            http_status = status.HTTP_404_NOT_FOUND
        elif isinstance(error, ApprovalRequestNotFound):
            response = ApiErrorResponse(
                error_code="approval_request_not_found",
                message=str(error),
                details={"request_id": str(error.request_id)},
            )
            http_status = status.HTTP_404_NOT_FOUND
        elif isinstance(error, ActionPlanNotCurrent):
            response = ApiErrorResponse(
                error_code="action_plan_not_current",
                message=str(error),
                details={
                    "plan_id": str(error.plan_id),
                    "current_plan_id": str(error.current_plan_id),
                },
            )
        elif isinstance(error, ActionPlanNotSubmittable):
            response = ApiErrorResponse(
                error_code="action_plan_not_submittable",
                message=str(error),
                details={"plan_id": str(error.plan_id), "status": error.status},
            )
        elif isinstance(error, ActionPlanDoesNotRequireApproval):
            response = ApiErrorResponse(
                error_code="action_plan_does_not_require_approval",
                message=str(error),
            )
        elif isinstance(error, ApprovalRequesterMustBeHuman):
            response = ApiErrorResponse(
                error_code="approval_requester_must_be_human",
                message=str(error),
            )
        elif isinstance(error, ApprovalDeciderMustBeHuman):
            response = ApiErrorResponse(
                error_code="approval_decider_must_be_human",
                message=str(error),
            )
        elif isinstance(error, ApprovalRequestAlreadyDecided):
            response = ApiErrorResponse(
                error_code="approval_request_already_decided",
                message=str(error),
                details={
                    "request_id": str(error.request_id),
                    "current_status": error.current_status,
                    "requested_decision": error.requested_decision,
                },
            )
        elif isinstance(error, ApprovalRequestExpired):
            response = ApiErrorResponse(
                error_code="approval_request_expired",
                message=str(error),
                details={
                    "request_id": str(error.request_id),
                    "expires_at": error.expires_at,
                },
            )
        elif isinstance(error, ActionPlanNotDecidable):
            response = ApiErrorResponse(
                error_code="action_plan_not_decidable",
                message=str(error),
                details={"plan_id": str(error.plan_id), "status": error.status},
            )
        elif isinstance(error, ApprovalPlanIntegrityViolation):
            response = ApiErrorResponse(
                error_code="approval_plan_integrity_violation",
                message=str(error),
                details={"request_id": str(error.request_id)},
            )
        else:
            response = ApiErrorResponse(error_code="approval_error", message=str(error))
        return JSONResponse(
            status_code=http_status,
            content=response.model_dump(mode="json"),
        )

    @app.exception_handler(ExecutionError)
    async def handle_execution_error(
        _: Request,
        error: ExecutionError,
    ) -> JSONResponse:
        http_status = status.HTTP_409_CONFLICT
        if isinstance(error, OperationStepNotFound):
            response = ApiErrorResponse(
                error_code="operation_step_not_found",
                message=str(error),
                details={"step_id": str(error.step_id)},
            )
            http_status = status.HTTP_404_NOT_FOUND
        elif isinstance(error, OperationExecutionNotFound):
            response = ApiErrorResponse(
                error_code="operation_execution_not_found",
                message=str(error),
                details={"execution_id": str(error.execution_id)},
            )
            http_status = status.HTTP_404_NOT_FOUND
        elif isinstance(error, OperationExecutionFailed):
            response = ApiErrorResponse(
                error_code="operation_execution_failed",
                message=str(error),
                details={
                    "execution_id": str(error.execution_id),
                    "operation_error_code": error.error_code,
                },
            )
            http_status = status.HTTP_502_BAD_GATEWAY
        elif isinstance(error, OperationNotAuthorized):
            response = ApiErrorResponse(
                error_code="operation_not_authorized",
                message=str(error),
                details={
                    "step_id": str(error.step_id),
                    "reason_code": error.reason_code,
                },
            )
        elif isinstance(error, OperationIdempotencyConflict):
            response = ApiErrorResponse(
                error_code="operation_idempotency_conflict",
                message=str(error),
                details={"idempotency_key": error.idempotency_key},
            )
        elif isinstance(error, OperationStepAlreadyRunning):
            response = ApiErrorResponse(
                error_code="operation_step_already_running",
                message=str(error),
                details={
                    "step_id": str(error.step_id),
                    "execution_id": str(error.execution_id),
                },
            )
        elif isinstance(error, OperationStepAlreadySucceeded):
            response = ApiErrorResponse(
                error_code="operation_step_already_succeeded",
                message=str(error),
                details={
                    "step_id": str(error.step_id),
                    "execution_id": str(error.execution_id),
                },
            )
        elif isinstance(error, ExecutionLeaseLost):
            response = ApiErrorResponse(
                error_code="execution_lease_lost",
                message=str(error),
                details={"execution_id": str(error.execution_id)},
            )
        else:
            response = ApiErrorResponse(error_code="execution_error", message=str(error))
        return JSONResponse(
            status_code=http_status,
            content=response.model_dump(mode="json"),
        )

    @app.exception_handler(VerificationError)
    async def handle_verification_error(
        _: Request,
        error: VerificationError,
    ) -> JSONResponse:
        http_status = status.HTTP_409_CONFLICT
        if isinstance(error, VerificationRunNotFound):
            response = ApiErrorResponse(
                error_code="verification_run_not_found",
                message=str(error),
                details={"run_id": str(error.run_id)},
            )
            http_status = status.HTTP_404_NOT_FOUND
        elif isinstance(error, OperationExecutionNotVerifiable):
            response = ApiErrorResponse(
                error_code="operation_execution_not_verifiable",
                message=str(error),
                details={
                    "execution_id": str(error.execution_id),
                    "reason_code": error.reason_code,
                },
            )
        elif isinstance(error, VerificationRunCompleted):
            response = ApiErrorResponse(
                error_code="verification_run_completed",
                message=str(error),
                details={
                    "run_id": str(error.run_id),
                    "status": error.status,
                },
            )
        elif isinstance(error, UnsupportedVerificationRule):
            response = ApiErrorResponse(
                error_code="unsupported_verification_rule",
                message=str(error),
                details={
                    "signal": error.signal,
                    "operator": error.operator,
                },
            )
        else:
            response = ApiErrorResponse(error_code="verification_error", message=str(error))
        return JSONResponse(
            status_code=http_status,
            content=response.model_dump(mode="json"),
        )

    return app
