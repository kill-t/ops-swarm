"""控制面 HTTP 错误转换；不参与领域判断或应用资源管理。"""

from typing import cast

from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import JsonValue

from opsswarm.approval import (
    ActionPlanDoesNotRequireApproval,
    ActionPlanNotCurrent,
    ActionPlanNotDecidable,
    ActionPlanNotExecutable,
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
from opsswarm.approval.candidate.contracts import RequestError
from opsswarm.approval.conditions.contracts import ApprovalError as ConditionApprovalError
from opsswarm.approval.decision.contracts import DecisionError
from opsswarm.approval.reinvestigation_candidate.contracts import ReflowRequestError
from opsswarm.approval.reinvestigation_decision.contracts import ReflowDecisionError
from opsswarm.control_plane.schemas import ApiErrorResponse
from opsswarm.evaluation.errors import EvaluationAttemptNotFound, EvaluationRunNotFound
from opsswarm.execution import (
    ExecutionError,
    ExecutionLeaseLost,
    OperationExecutionFailed,
    OperationExecutionNotFound,
    OperationIdempotencyConflict,
    OperationNotAuthorized,
    OperationStepAlreadyRunning,
    OperationStepAlreadySucceeded,
    OperationStepNotFound,
)
from opsswarm.execution.candidate.contracts import PreflightError
from opsswarm.execution.conditions.contracts import ConditionError
from opsswarm.execution.release.contracts import ReleaseRequestError
from opsswarm.execution.release_jobs.contracts import ExecutionJobError
from opsswarm.experience.contracts import ExperienceError
from opsswarm.follow_up import (
    FollowUpReportNotFound,
    ReportActionItemAlreadyCompleted,
    ReportActionItemError,
    ReportActionItemIdempotencyConflict,
    ReportActionItemNotFound,
    ReportActionItemValidationError,
    ReportActionItemVersionConflict,
)
from opsswarm.identity.models import OperatorAccessError
from opsswarm.incidents import (
    IncidentDomainError,
    IncidentNotFound,
    IncidentValidationError,
    IncidentVersionConflict,
    InvalidIncidentTransition,
)
from opsswarm.incidents.convergence.contracts import ConvergenceError
from opsswarm.investigation.candidate.contracts import CandidateError
from opsswarm.investigation.errors import InvestigationError, InvestigationRunNotFound
from opsswarm.investigation.human_prepare.contracts import PreparationError
from opsswarm.investigation.human_review.contracts import HumanReviewError
from opsswarm.investigation.human_routing.contracts import HumanRoutingError
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.investigation.reinvestigation.contracts import ReinvestigationError
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.settlement.contracts import SettlementError
from opsswarm.planning import (
    ActionPlanCapabilityUnavailable,
    ActionPlanNotFound,
    ActionPlanNotReady,
    HypothesisNotSupported,
    PlanningError,
    UnsafeConfigurationRollbackTarget,
    UnsafeDependencyRestoreTarget,
    UnsafeRollbackTarget,
    UnsupportedPlanningRootCause,
)
from opsswarm.planning.candidate.contracts import PlanError
from opsswarm.planning.reinvestigation_candidate.contracts import ReflowPlanError
from opsswarm.reports import (
    IncidentReportError,
    IncidentReportIntegrityViolation,
    IncidentReportNotFound,
    IncidentReportNotReady,
    IncidentReportTooLarge,
)
from opsswarm.verification import (
    OperationExecutionNotVerifiable,
    UnsupportedVerificationRule,
    VerificationError,
    VerificationRunCompleted,
    VerificationRunNotFound,
    VerificationWorkItemNotFound,
)
from opsswarm.verification.candidate.contracts import RecoveryError


def _json_response(
    response: ApiErrorResponse,
    status_code: int,
    *,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """统一序列化公开错误，认证响应头由明确的映射单独提供。"""
    return JSONResponse(
        status_code=status_code,
        content=response.model_dump(mode="json"),
        headers=headers,
    )


type CodedError = (
    HumanWaitError
    | HumanReviewError
    | HumanRoutingError
    | CandidateError
    | PlanError
    | ReflowPlanError
    | RequestError
    | ReflowRequestError
    | ReflowDecisionError
    | DecisionError
    | PreflightError
    | ConditionError
    | ConditionApprovalError
    | ExecutionJobError
    | RecoveryError
    | SettlementError
    | ConvergenceError
    | ReinvestigationError
    | ReflowError
    | ReleaseRequestError
    | PreparationError
)


async def handle_coded_error(_: Request, error: CodedError) -> JSONResponse:
    return _json_response(
        ApiErrorResponse(error_code=error.code, message=str(error)), error.status_code
    )


async def handle_experience_error(_: Request, error: ExperienceError) -> JSONResponse:
    response = ApiErrorResponse(error_code=error.code, message=error.message)
    return _json_response(response, error.status_code)


async def handle_operator_access_error(
    _: Request,
    error: OperatorAccessError,
) -> JSONResponse:
    response = ApiErrorResponse(error_code=error.code, message=str(error))
    return _json_response(
        response,
        error.status_code,
        headers={"WWW-Authenticate": "Bearer"} if error.status_code == 401 else None,
    )


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
    return _json_response(response, status.HTTP_422_UNPROCESSABLE_CONTENT)


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
        return _json_response(response, status.HTTP_404_NOT_FOUND)
    if isinstance(error, IncidentVersionConflict):
        response = ApiErrorResponse(
            error_code="incident_version_conflict",
            message=str(error),
            details={
                "expected_version": error.expected_version,
                "actual_version": error.actual_version,
            },
        )
        return _json_response(response, status.HTTP_409_CONFLICT)
    if isinstance(error, InvalidIncidentTransition):
        response = ApiErrorResponse(
            error_code="invalid_incident_transition",
            message=str(error),
            details={"status": error.status, "action": error.action},
        )
        return _json_response(response, status.HTTP_409_CONFLICT)
    if isinstance(error, IncidentValidationError):
        response = ApiErrorResponse(
            error_code="incident_validation_error",
            message=str(error),
        )
        return _json_response(response, status.HTTP_422_UNPROCESSABLE_CONTENT)

    response = ApiErrorResponse(error_code="incident_domain_error", message=str(error))
    return _json_response(response, status.HTTP_400_BAD_REQUEST)


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
        return _json_response(response, status.HTTP_404_NOT_FOUND)
    response = ApiErrorResponse(error_code="investigation_error", message=str(error))
    return _json_response(response, status.HTTP_400_BAD_REQUEST)


async def handle_evaluation_run_not_found(
    _: Request,
    error: EvaluationRunNotFound,
) -> JSONResponse:
    response = ApiErrorResponse(
        error_code="evaluation_run_not_found",
        message=str(error),
        details={"run_id": str(error.run_id)},
    )
    return _json_response(response, status.HTTP_404_NOT_FOUND)


async def handle_evaluation_attempt_not_found(
    _: Request,
    error: EvaluationAttemptNotFound,
) -> JSONResponse:
    response = ApiErrorResponse(
        error_code="evaluation_attempt_not_found",
        message=str(error),
        details={"attempt_id": str(error.attempt_id)},
    )
    return _json_response(response, status.HTTP_404_NOT_FOUND)


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
        return _json_response(response, status.HTTP_404_NOT_FOUND)
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
    elif isinstance(error, ActionPlanCapabilityUnavailable):
        response = ApiErrorResponse(
            error_code="action_plan_capability_unavailable",
            message=str(error),
            details=error.capability.model_dump(mode="json"),
        )
    elif isinstance(error, UnsafeRollbackTarget):
        response = ApiErrorResponse(
            error_code="unsafe_rollback_target",
            message=str(error),
        )
    elif isinstance(error, UnsafeConfigurationRollbackTarget):
        response = ApiErrorResponse(
            error_code="unsafe_configuration_rollback_target",
            message=str(error),
        )
    elif isinstance(error, UnsafeDependencyRestoreTarget):
        response = ApiErrorResponse(
            error_code="unsafe_dependency_restore_target",
            message=str(error),
        )
    else:
        response = ApiErrorResponse(error_code="planning_error", message=str(error))
    return _json_response(response, status.HTTP_409_CONFLICT)


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
    elif isinstance(error, ActionPlanNotExecutable):
        response = ApiErrorResponse(
            error_code="action_plan_not_executable",
            message=str(error),
            details={
                "plan_id": str(error.plan_id),
                "action_type": error.action_type,
            },
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
    return _json_response(response, http_status)


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
    return _json_response(response, http_status)


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
    elif isinstance(error, VerificationWorkItemNotFound):
        response = ApiErrorResponse(
            error_code="verification_work_item_not_found",
            message=str(error),
            details={"work_item_id": str(error.work_item_id)},
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
    return _json_response(response, http_status)


async def handle_incident_report_error(
    _: Request,
    error: IncidentReportError,
) -> JSONResponse:
    http_status = status.HTTP_409_CONFLICT
    if isinstance(error, IncidentReportNotFound):
        response = ApiErrorResponse(
            error_code="incident_report_not_found",
            message=str(error),
            details={"incident_id": str(error.incident_id)},
        )
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(error, IncidentReportNotReady):
        response = ApiErrorResponse(
            error_code="incident_report_not_ready",
            message=str(error),
            details={
                "incident_id": str(error.incident_id),
                "status": error.status,
            },
        )
    elif isinstance(error, IncidentReportTooLarge):
        response = ApiErrorResponse(
            error_code="incident_report_too_large",
            message=str(error),
            details={
                "incident_id": str(error.incident_id),
                "maximum": error.maximum,
            },
        )
    elif isinstance(error, IncidentReportIntegrityViolation):
        response = ApiErrorResponse(
            error_code="incident_report_integrity_violation",
            message=str(error),
            details={"report_id": str(error.report_id)},
        )
        http_status = status.HTTP_500_INTERNAL_SERVER_ERROR
    else:
        response = ApiErrorResponse(error_code="incident_report_error", message=str(error))
    return _json_response(response, http_status)


async def handle_report_action_item_error(
    _: Request,
    error: ReportActionItemError,
) -> JSONResponse:
    """把改进行动项领域错误映射为稳定 HTTP 契约。"""

    http_status = status.HTTP_409_CONFLICT
    if isinstance(error, FollowUpReportNotFound):
        response = ApiErrorResponse(
            error_code="follow_up_report_not_found",
            message=str(error),
            details={"report_id": str(error.report_id)},
        )
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(error, ReportActionItemNotFound):
        response = ApiErrorResponse(
            error_code="report_action_item_not_found",
            message=str(error),
            details={"item_id": str(error.item_id)},
        )
        http_status = status.HTTP_404_NOT_FOUND
    elif isinstance(error, ReportActionItemValidationError):
        response = ApiErrorResponse(
            error_code="report_action_item_validation_error",
            message=str(error),
        )
        http_status = status.HTTP_422_UNPROCESSABLE_CONTENT
    elif isinstance(error, ReportActionItemVersionConflict):
        response = ApiErrorResponse(
            error_code="report_action_item_version_conflict",
            message=str(error),
            details={
                "expected_version": error.expected_version,
                "actual_version": error.actual_version,
            },
        )
    elif isinstance(error, ReportActionItemIdempotencyConflict):
        response = ApiErrorResponse(
            error_code="report_action_item_idempotency_conflict",
            message=str(error),
            details={
                "report_id": str(error.report_id),
                "idempotency_key": error.idempotency_key,
            },
        )
    elif isinstance(error, ReportActionItemAlreadyCompleted):
        response = ApiErrorResponse(
            error_code="report_action_item_already_completed",
            message=str(error),
            details={"item_id": str(error.item_id)},
        )
    else:
        response = ApiErrorResponse(
            error_code="report_action_item_error",
            message=str(error),
        )
    return _json_response(response, http_status)


def register_exception_handlers(app: FastAPI) -> None:
    """只注册已知错误，保留框架对未预期异常和 HTTP 异常的处理。"""
    for error_type in (
        HumanWaitError,
        HumanReviewError,
        HumanRoutingError,
        CandidateError,
        PlanError,
        ReflowPlanError,
        RequestError,
        ReflowRequestError,
        ReflowDecisionError,
        DecisionError,
        PreflightError,
        ConditionError,
        ConditionApprovalError,
        ExecutionJobError,
        RecoveryError,
        SettlementError,
        ConvergenceError,
        ReinvestigationError,
        ReflowError,
        ReleaseRequestError,
        PreparationError,
    ):
        app.exception_handler(error_type)(handle_coded_error)
    app.exception_handler(ExperienceError)(handle_experience_error)
    app.exception_handler(OperatorAccessError)(handle_operator_access_error)
    app.exception_handler(RequestValidationError)(handle_request_validation_error)
    app.exception_handler(IncidentDomainError)(handle_incident_domain_error)
    app.exception_handler(InvestigationError)(handle_investigation_error)
    app.exception_handler(EvaluationRunNotFound)(handle_evaluation_run_not_found)
    app.exception_handler(EvaluationAttemptNotFound)(handle_evaluation_attempt_not_found)
    app.exception_handler(PlanningError)(handle_planning_error)
    app.exception_handler(ApprovalError)(handle_approval_error)
    app.exception_handler(ExecutionError)(handle_execution_error)
    app.exception_handler(VerificationError)(handle_verification_error)
    app.exception_handler(IncidentReportError)(handle_incident_report_error)
    app.exception_handler(ReportActionItemError)(handle_report_action_item_error)
