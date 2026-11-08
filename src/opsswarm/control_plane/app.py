"""OpsSwarm 控制面 FastAPI 应用工厂。"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import cast

from fastapi import Depends, FastAPI, Request, Response
from starlette.middleware.base import RequestResponseEndpoint

from opsswarm import __version__
from opsswarm.approval.candidate.api import create_candidate_approval_router
from opsswarm.approval.candidate.store import CandidateApprovalStore
from opsswarm.approval.conditions.api import create_condition_approval_router
from opsswarm.approval.conditions.store import ConditionApprovalStore
from opsswarm.approval.decision.api import create_candidate_decision_router
from opsswarm.approval.decision.store import CandidateDecisionStore
from opsswarm.approval.persistence import ApprovalRequestStore
from opsswarm.approval.reinvestigation_candidate.api import create_reflow_approval_router
from opsswarm.approval.reinvestigation_candidate.store import ReflowApprovalStore
from opsswarm.approval.reinvestigation_decision.api import create_reflow_approval_decision_router
from opsswarm.approval.reinvestigation_decision.store import ReflowApprovalDecisionStore
from opsswarm.control_plane.collaboration import create_collaboration_navigation_router
from opsswarm.control_plane.error_handlers import register_exception_handlers
from opsswarm.control_plane.routes import Clock, create_router
from opsswarm.control_plane.schemas import ApiErrorResponse
from opsswarm.database import Database, DatabaseSettings
from opsswarm.evaluation.persistence import EvaluationStore
from opsswarm.execution import (
    FaultLabOperationAdapter,
    OperationAdapter,
)
from opsswarm.execution.candidate.api import create_candidate_preflight_router
from opsswarm.execution.candidate.store import CandidatePreflightStore
from opsswarm.execution.conditions.api import create_candidate_condition_router
from opsswarm.execution.conditions.store import CandidateConditionStore
from opsswarm.execution.persistence import OperationExecutionStore
from opsswarm.execution.release.api import create_candidate_release_router
from opsswarm.execution.release.store import CandidateReleaseRequestStore
from opsswarm.execution.release_jobs.api import create_release_execution_router
from opsswarm.execution.release_jobs.store import ReleaseExecutionStore
from opsswarm.execution.service import OperationExecutionService
from opsswarm.experience.api import create_experience_router
from opsswarm.experience.lifecycle_api import create_experience_lifecycle_router
from opsswarm.experience.lifecycle_store import ExperienceLifecycleStore
from opsswarm.experience.replacement_api import create_experience_replacement_router
from opsswarm.experience.replacement_store import ExperienceReplacementStore
from opsswarm.experience.review_api import create_experience_review_router
from opsswarm.experience.review_store import ExperienceReviewStore
from opsswarm.experience.search_store import ExperienceSearchStore
from opsswarm.experience.store import ExperienceStore
from opsswarm.follow_up.persistence import ReportActionItemStore
from opsswarm.identity.api import authorize_control_request, create_identity_router
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.identity.store import OperatorAuthorizationStore
from opsswarm.incidents.convergence.api import create_candidate_incident_convergence_router
from opsswarm.incidents.convergence.store import CandidateIncidentConvergenceStore
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.budget.api import create_call_budget_router
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.candidate.api import create_candidate_router
from opsswarm.investigation.candidate.store import CandidateStore
from opsswarm.investigation.context.evidence_api import create_context_evidence_router
from opsswarm.investigation.human_prepare.api import create_human_preparation_router
from opsswarm.investigation.human_prepare.store import HumanPreparationStore
from opsswarm.investigation.human_review.api import create_human_review_router
from opsswarm.investigation.human_review.store import HumanReviewStore
from opsswarm.investigation.human_wait.api import create_human_wait_router
from opsswarm.investigation.human_wait.store import HumanWaitStore
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.investigation.reinvestigation.api import create_candidate_reinvestigation_router
from opsswarm.investigation.reinvestigation.store import CandidateReinvestigationStore
from opsswarm.investigation.reinvestigation_candidate.api import create_reflow_candidate_router
from opsswarm.investigation.reinvestigation_candidate.queries import ReflowCandidateQueries
from opsswarm.investigation.reinvestigation_coordination.api import create_reflow_router
from opsswarm.investigation.reinvestigation_coordination.queries import ReflowQueries
from opsswarm.investigation.reinvestigation_dispatch.api import create_dispatch_router
from opsswarm.investigation.reinvestigation_dispatch.queries import DispatchQueries
from opsswarm.investigation.reinvestigation_execution.api import create_execution_router
from opsswarm.investigation.reinvestigation_execution.queries import ExecutionQueries
from opsswarm.investigation.reinvestigation_review.api import create_reflow_review_router
from opsswarm.investigation.reinvestigation_review.queries import ReflowReviewQueries
from opsswarm.investigation.reinvestigation_routing.api import create_reflow_routing_router
from opsswarm.investigation.reinvestigation_routing.queries import ReflowRoutingQueries
from opsswarm.investigation.reinvestigation_synthesis.api import create_reflow_synthesis_router
from opsswarm.investigation.reinvestigation_synthesis.queries import SynthesisQueries
from opsswarm.investigation.settlement.api import create_candidate_settlement_router
from opsswarm.investigation.settlement.store import CandidateSettlementStore
from opsswarm.investigation.tasks.api import create_collaboration_router
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.observability import (
    TracingRuntime,
    create_tracing_runtime,
    current_trace_id_hex,
)
from opsswarm.planning.candidate.api import create_candidate_plan_router
from opsswarm.planning.candidate.store import CandidatePlanStore
from opsswarm.planning.persistence import ActionPlanStore
from opsswarm.planning.reinvestigation_candidate.api import create_reflow_plan_router
from opsswarm.planning.reinvestigation_candidate.store import ReflowPlanStore
from opsswarm.reports.persistence import IncidentReportStore
from opsswarm.verification import (
    PrometheusVerificationAdapter,
    VerificationAdapter,
)
from opsswarm.verification.candidate.api import create_candidate_recovery_router
from opsswarm.verification.candidate.store import CandidateRecoveryStore
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
    incident_report_store: IncidentReportStore | None = None,
    report_action_item_store: ReportActionItemStore | None = None,
    collaboration_queries: CollaborationQueries | None = None,
    call_budget_queries: CallBudgetQueries | None = None,
    database_settings: DatabaseSettings | None = None,
    operator_auth_settings: OperatorAuthSettings | None = None,
    operator_authorization_store: OperatorAuthorizationStore | None = None,
    human_wait_store: HumanWaitStore | None = None,
    human_preparation_store: HumanPreparationStore | None = None,
    reflow_plan_store: ReflowPlanStore | None = None,
    candidate_plan_store: CandidatePlanStore | None = None,
    reflow_approval_store: ReflowApprovalStore | None = None,
    reflow_approval_decision_store: ReflowApprovalDecisionStore | None = None,
    candidate_approval_store: CandidateApprovalStore | None = None,
    candidate_decision_store: CandidateDecisionStore | None = None,
    candidate_preflight_store: CandidatePreflightStore | None = None,
    candidate_condition_store: CandidateConditionStore | None = None,
    candidate_release_request_store: CandidateReleaseRequestStore | None = None,
    release_execution_store: ReleaseExecutionStore | None = None,
    candidate_recovery_store: CandidateRecoveryStore | None = None,
    candidate_settlement_store: CandidateSettlementStore | None = None,
    candidate_incident_convergence_store: CandidateIncidentConvergenceStore | None = None,
    candidate_reinvestigation_store: CandidateReinvestigationStore | None = None,
    reflow_queries: ReflowQueries | None = None,
    dispatch_queries: DispatchQueries | None = None,
    execution_queries: ExecutionQueries | None = None,
    reflow_synthesis_queries: SynthesisQueries | None = None,
    reflow_candidate_queries: ReflowCandidateQueries | None = None,
    reflow_review_queries: ReflowReviewQueries | None = None,
    reflow_routing_queries: ReflowRoutingQueries | None = None,
    condition_approval_store: ConditionApprovalStore | None = None,
    candidate_store: CandidateStore | None = None,
    human_review_store: HumanReviewStore | None = None,
    experience_store: ExperienceStore | None = None,
    experience_review_store: ExperienceReviewStore | None = None,
    experience_lifecycle_store: ExperienceLifecycleStore | None = None,
    experience_search_store: ExperienceSearchStore | None = None,
    experience_replacement_store: ExperienceReplacementStore | None = None,
    tracing_runtime: TracingRuntime | None = None,
    clock: Clock = utc_now,
) -> FastAPI:
    """创建生产可管理数据库资源、测试可注入仓储的控制面应用。"""

    selected_tracing_runtime = tracing_runtime or create_tracing_runtime("opsswarm-control-plane")
    selected_auth_settings = (
        operator_auth_settings if operator_auth_settings is not None else OperatorAuthSettings()
    )

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
                    else PrometheusVerificationAdapter()
                ),
                clock=clock,
            )
            application.state.workroom_store = workroom_store
            application.state.evaluation_store = evaluation_store
            application.state.incident_report_store = incident_report_store
            application.state.report_action_item_store = report_action_item_store
            application.state.collaboration_queries = collaboration_queries
            application.state.call_budget_queries = call_budget_queries
            application.state.human_wait_store = human_wait_store
            application.state.human_preparation_store = human_preparation_store
            application.state.condition_approval_store = condition_approval_store
            application.state.candidate_release_request_store = candidate_release_request_store
            application.state.reflow_routing_queries = reflow_routing_queries
            application.state.reflow_candidate_queries = reflow_candidate_queries
            application.state.reflow_review_queries = reflow_review_queries
            application.state.reflow_synthesis_queries = reflow_synthesis_queries
            application.state.execution_queries = execution_queries
            application.state.dispatch_queries = dispatch_queries
            application.state.reflow_queries = reflow_queries
            application.state.candidate_reinvestigation_store = candidate_reinvestigation_store
            application.state.candidate_incident_convergence_store = (
                candidate_incident_convergence_store
            )
            application.state.candidate_settlement_store = candidate_settlement_store
            application.state.candidate_recovery_store = candidate_recovery_store
            application.state.release_execution_store = release_execution_store
            application.state.candidate_condition_store = candidate_condition_store
            application.state.candidate_preflight_store = candidate_preflight_store
            application.state.candidate_decision_store = candidate_decision_store
            application.state.reflow_approval_store = reflow_approval_store
            application.state.reflow_approval_decision_store = reflow_approval_decision_store
            application.state.candidate_approval_store = candidate_approval_store
            application.state.reflow_plan_store = reflow_plan_store
            application.state.candidate_plan_store = candidate_plan_store
            application.state.candidate_store = candidate_store
            application.state.human_review_store = human_review_store
            application.state.experience_store = experience_store
            application.state.experience_review_store = experience_review_store
            application.state.experience_lifecycle_store = experience_lifecycle_store
            application.state.experience_search_store = experience_search_store
            application.state.experience_replacement_store = experience_replacement_store
            try:
                yield
            finally:
                selected_tracing_runtime.shutdown()
            return

        settings = database_settings if database_settings is not None else DatabaseSettings()
        database = Database(settings.database_url)
        application.state.database = database
        application.state.human_wait_store = human_wait_store or HumanWaitStore(
            database.session_factory, clock=clock
        )
        application.state.human_preparation_store = (
            human_preparation_store or HumanPreparationStore(database.session_factory, clock=clock)
        )
        application.state.condition_approval_store = (
            condition_approval_store
            or ConditionApprovalStore(database.session_factory, clock=clock)
        )
        application.state.candidate_release_request_store = (
            candidate_release_request_store
            or CandidateReleaseRequestStore(database.session_factory, clock=clock)
        )
        application.state.reflow_routing_queries = reflow_routing_queries or ReflowRoutingQueries(
            database.session_factory, clock=clock
        )
        application.state.reflow_candidate_queries = (
            reflow_candidate_queries
            or ReflowCandidateQueries(database.session_factory, clock=clock)
        )
        application.state.reflow_review_queries = reflow_review_queries or ReflowReviewQueries(
            database.session_factory, clock=clock
        )
        application.state.reflow_synthesis_queries = reflow_synthesis_queries or SynthesisQueries(
            database.session_factory, clock=clock
        )
        application.state.execution_queries = execution_queries or ExecutionQueries(
            database.session_factory, clock=clock
        )
        application.state.dispatch_queries = dispatch_queries or DispatchQueries(
            database.session_factory, clock=clock
        )
        application.state.reflow_queries = reflow_queries or ReflowQueries(
            database.session_factory, clock=clock
        )
        application.state.candidate_reinvestigation_store = (
            candidate_reinvestigation_store
            or CandidateReinvestigationStore(database.session_factory, clock=clock)
        )
        application.state.candidate_incident_convergence_store = (
            candidate_incident_convergence_store
            or CandidateIncidentConvergenceStore(database.session_factory, clock=clock)
        )
        application.state.candidate_settlement_store = (
            candidate_settlement_store
            or CandidateSettlementStore(database.session_factory, clock=clock)
        )
        application.state.candidate_recovery_store = (
            candidate_recovery_store
            or CandidateRecoveryStore(database.session_factory, clock=clock)
        )
        application.state.release_execution_store = (
            release_execution_store
            or ReleaseExecutionStore(
                database.session_factory, auth_provider=lambda: selected_auth_settings, clock=clock
            )
        )
        application.state.candidate_condition_store = (
            candidate_condition_store
            or CandidateConditionStore(database.session_factory, clock=clock)
        )
        application.state.candidate_preflight_store = (
            candidate_preflight_store
            or CandidatePreflightStore(database.session_factory, clock=clock)
        )
        application.state.candidate_decision_store = (
            candidate_decision_store
            or CandidateDecisionStore(database.session_factory, clock=clock)
        )
        application.state.reflow_approval_decision_store = (
            reflow_approval_decision_store
            or ReflowApprovalDecisionStore(database.session_factory, clock=clock)
        )
        application.state.reflow_approval_store = reflow_approval_store or ReflowApprovalStore(
            database.session_factory, clock=clock
        )
        application.state.candidate_approval_store = (
            candidate_approval_store
            or CandidateApprovalStore(database.session_factory, clock=clock)
        )
        application.state.reflow_plan_store = reflow_plan_store or ReflowPlanStore(
            database.session_factory, clock=clock
        )
        application.state.candidate_plan_store = candidate_plan_store or CandidatePlanStore(
            database.session_factory, clock=clock
        )
        application.state.candidate_store = candidate_store or CandidateStore(
            database.session_factory, clock=clock
        )
        application.state.human_review_store = human_review_store or HumanReviewStore(
            database.session_factory, clock=clock
        )
        application.state.experience_store = experience_store or ExperienceStore(
            database.session_factory, clock=clock
        )
        application.state.experience_review_store = (
            experience_review_store or ExperienceReviewStore(database.session_factory, clock=clock)
        )
        application.state.experience_lifecycle_store = (
            experience_lifecycle_store
            or ExperienceLifecycleStore(database.session_factory, clock=clock)
        )
        application.state.experience_search_store = (
            experience_search_store or ExperienceSearchStore(database.session_factory, clock=clock)
        )
        application.state.experience_replacement_store = (
            experience_replacement_store
            or ExperienceReplacementStore(database.session_factory, clock=clock)
        )
        application.state.operator_authorization_store = OperatorAuthorizationStore(
            database.session_factory
        )
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
                else PrometheusVerificationAdapter()
            ),
            clock=clock,
        )
        application.state.workroom_store = WorkroomStore(database.session_factory)
        application.state.evaluation_store = EvaluationStore(database.session_factory)
        application.state.incident_report_store = IncidentReportStore(database.session_factory)
        application.state.report_action_item_store = ReportActionItemStore(database.session_factory)
        application.state.collaboration_queries = CollaborationQueries(database.session_factory)
        application.state.call_budget_queries = CallBudgetQueries(database.session_factory)
        try:
            yield
        finally:
            await database.dispose()
            selected_tracing_runtime.shutdown()

    app = FastAPI(
        title="OpsSwarm Control Plane",
        version=__version__,
        description="事故接收、调查状态与不可变时间线控制面。仅限本地 MVP 演练环境。",
        lifespan=lifespan,
        dependencies=[Depends(authorize_control_request)],
        responses={
            401: {"model": ApiErrorResponse, "description": "需要有效操作者认证"},
            403: {"model": ApiErrorResponse, "description": "操作者越权或身份声明不符"},
            503: {"model": ApiErrorResponse, "description": "授权审计不可用，命令未执行"},
        },
    )
    app.state.human_wait_store = human_wait_store
    app.state.human_preparation_store = human_preparation_store
    app.state.condition_approval_store = condition_approval_store
    app.state.candidate_release_request_store = candidate_release_request_store
    app.state.reflow_routing_queries = reflow_routing_queries
    app.state.reflow_candidate_queries = reflow_candidate_queries
    app.state.reflow_review_queries = reflow_review_queries
    app.state.reflow_synthesis_queries = reflow_synthesis_queries
    app.state.execution_queries = execution_queries
    app.state.dispatch_queries = dispatch_queries
    app.state.reflow_queries = reflow_queries
    app.state.candidate_reinvestigation_store = candidate_reinvestigation_store
    app.state.candidate_incident_convergence_store = candidate_incident_convergence_store
    app.state.candidate_settlement_store = candidate_settlement_store
    app.state.candidate_recovery_store = candidate_recovery_store
    app.state.release_execution_store = release_execution_store
    app.state.candidate_condition_store = candidate_condition_store
    app.state.candidate_preflight_store = candidate_preflight_store
    app.state.candidate_decision_store = candidate_decision_store
    app.state.reflow_approval_store = reflow_approval_store
    app.state.reflow_approval_decision_store = reflow_approval_decision_store
    app.state.candidate_approval_store = candidate_approval_store
    app.state.reflow_plan_store = reflow_plan_store
    app.state.candidate_plan_store = candidate_plan_store
    app.state.candidate_store = candidate_store
    app.state.human_review_store = human_review_store
    app.state.incident_store = incident_store
    app.state.investigation_store = investigation_store
    app.state.action_plan_store = action_plan_store
    app.state.approval_request_store = approval_request_store
    app.state.operation_execution_service = None
    app.state.verification_service = None
    app.state.workroom_store = workroom_store
    app.state.evaluation_store = evaluation_store
    app.state.incident_report_store = incident_report_store
    app.state.report_action_item_store = report_action_item_store
    app.state.collaboration_queries = collaboration_queries
    app.state.call_budget_queries = call_budget_queries
    app.state.operator_auth_settings = selected_auth_settings
    app.state.operator_clock = clock
    app.state.operator_authorization_store = operator_authorization_store
    app.include_router(create_human_wait_router())
    app.include_router(create_human_preparation_router())
    app.include_router(create_human_review_router())
    app.include_router(create_candidate_router())
    app.include_router(create_condition_approval_router())
    app.include_router(create_candidate_release_router())
    app.include_router(create_release_execution_router())
    app.include_router(create_candidate_recovery_router())
    app.include_router(create_candidate_settlement_router())
    app.include_router(create_candidate_incident_convergence_router())
    app.include_router(create_candidate_reinvestigation_router())
    app.include_router(create_reflow_router())
    app.include_router(create_dispatch_router())
    app.include_router(create_execution_router())
    app.include_router(create_reflow_synthesis_router())
    app.include_router(create_reflow_candidate_router())
    app.include_router(create_reflow_review_router())
    app.include_router(create_reflow_routing_router())
    app.include_router(create_candidate_condition_router())
    app.include_router(create_candidate_preflight_router())
    app.include_router(create_candidate_decision_router())
    app.include_router(create_reflow_approval_router())
    app.include_router(create_reflow_approval_decision_router())
    app.include_router(create_candidate_approval_router())
    app.include_router(create_reflow_plan_router())
    app.include_router(create_candidate_plan_router())
    app.include_router(create_identity_router())
    app.include_router(create_collaboration_navigation_router())
    app.include_router(create_experience_router())
    app.include_router(create_experience_review_router())
    app.include_router(create_experience_lifecycle_router())
    app.include_router(create_experience_replacement_router())
    app.include_router(create_router(clock))
    app.include_router(create_collaboration_router())
    app.include_router(create_call_budget_router())
    app.include_router(create_context_evidence_router())

    @app.middleware("http")
    async def add_trace_id_header(
        request: Request,
        call_next: RequestResponseEndpoint,
    ) -> Response:
        """追踪开启时让调用方能用响应头定位 Jaeger trace。"""

        response = await call_next(request)
        authorization_id = getattr(request.state, "authorization_id", None)
        if authorization_id is not None:
            response.headers["X-Authorization-ID"] = str(authorization_id)
        trace_id = current_trace_id_hex()
        if trace_id is not None:
            response.headers["X-Trace-ID"] = trace_id
        return response

    selected_tracing_runtime.instrument_fastapi(app)

    register_exception_handlers(app)

    return app
