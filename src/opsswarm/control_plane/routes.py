"""控制面 Incident HTTP 路由。"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request, Response, status
from fastapi.responses import StreamingResponse

from opsswarm.approval import ApprovalDecision, ApprovalRequestExpired, ApprovalRequestStatus
from opsswarm.approval.persistence import ApprovalRequestStore
from opsswarm.control_plane.schemas import (
    ActionPlanCapabilityResponse,
    ActionPlanResponse,
    AlertIngestionResponse,
    AlertRequest,
    ApiErrorResponse,
    ApprovalRequestResponse,
    CloseIncidentRequest,
    CreateActionPlanResponse,
    CreateApprovalRequest,
    CreateApprovalRequestResponse,
    CreateVerificationRunRequest,
    CreateVerificationRunResponse,
    DecideApprovalRequest,
    DecideApprovalRequestResponse,
    EvaluationAttemptPageResponse,
    EvaluationAttemptResponse,
    EvaluationRunPageResponse,
    EvaluationRunResponse,
    EvidenceBoardResponse,
    ExecuteOperationRequest,
    ExecuteOperationResponse,
    HealthResponse,
    IncidentEventPageResponse,
    IncidentEventResponse,
    IncidentPageResponse,
    IncidentResponse,
    IncidentWorkroomResponse,
    InvestigationRunResponse,
    OperationExecutionResponse,
    RecordVerificationSampleResponse,
    StartInvestigationRequest,
    StartInvestigationResponse,
    VerificationRunResponse,
    VerificationSampleRequest,
    VerificationWorkItemResponse,
)
from opsswarm.control_plane.sse import IncidentEventStream
from opsswarm.evaluation.persistence import EvaluationStore
from opsswarm.execution import ExecutionStatus, OperationExecutionFailed
from opsswarm.execution.service import OperationExecutionService
from opsswarm.incidents import ActorType, IncidentAction, IncidentActor, IncidentStatus
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.planning.persistence import ActionPlanStore
from opsswarm.verification.service import VerificationService
from opsswarm.workroom import WorkroomStore

Clock = Callable[[], datetime]


def get_incident_store(request: Request) -> IncidentStore:
    """从应用生命周期状态获取仓储。"""

    store = getattr(request.app.state, "incident_store", None)
    if not isinstance(store, IncidentStore):
        raise RuntimeError("IncidentStore 尚未初始化")
    return store


IncidentStoreDependency = Annotated[IncidentStore, Depends(get_incident_store)]


def get_investigation_store(request: Request) -> InvestigationStore:
    """从应用生命周期状态获取调查仓储。"""

    store = getattr(request.app.state, "investigation_store", None)
    if not isinstance(store, InvestigationStore):
        raise RuntimeError("InvestigationStore 尚未初始化")
    return store


InvestigationStoreDependency = Annotated[InvestigationStore, Depends(get_investigation_store)]


def get_action_plan_store(request: Request) -> ActionPlanStore:
    """从应用生命周期状态获取处置计划仓储。"""

    store = getattr(request.app.state, "action_plan_store", None)
    if not isinstance(store, ActionPlanStore):
        raise RuntimeError("ActionPlanStore 尚未初始化")
    return store


ActionPlanStoreDependency = Annotated[ActionPlanStore, Depends(get_action_plan_store)]


def get_approval_request_store(request: Request) -> ApprovalRequestStore:
    """从应用生命周期状态获取审批请求仓储。"""

    store = getattr(request.app.state, "approval_request_store", None)
    if not isinstance(store, ApprovalRequestStore):
        raise RuntimeError("ApprovalRequestStore 尚未初始化")
    return store


ApprovalRequestStoreDependency = Annotated[
    ApprovalRequestStore,
    Depends(get_approval_request_store),
]


def get_operation_execution_service(request: Request) -> OperationExecutionService:
    """从应用生命周期状态获取确定性执行服务。"""

    service = getattr(request.app.state, "operation_execution_service", None)
    if not isinstance(service, OperationExecutionService):
        raise RuntimeError("OperationExecutionService 尚未初始化")
    return service


OperationExecutionServiceDependency = Annotated[
    OperationExecutionService,
    Depends(get_operation_execution_service),
]


def get_verification_service(request: Request) -> VerificationService:
    """从应用生命周期状态获取确定性恢复验证服务。"""

    service = getattr(request.app.state, "verification_service", None)
    if not isinstance(service, VerificationService):
        raise RuntimeError("VerificationService 尚未初始化")
    return service


VerificationServiceDependency = Annotated[
    VerificationService,
    Depends(get_verification_service),
]


def get_workroom_store(request: Request) -> WorkroomStore:
    """从应用生命周期状态获取只读作战室投影仓储。"""

    store = getattr(request.app.state, "workroom_store", None)
    if not isinstance(store, WorkroomStore):
        raise RuntimeError("WorkroomStore 尚未初始化")
    return store


WorkroomStoreDependency = Annotated[WorkroomStore, Depends(get_workroom_store)]


def get_evaluation_store(request: Request) -> EvaluationStore:
    """从应用生命周期状态获取只读评测仓储。"""

    store = getattr(request.app.state, "evaluation_store", None)
    if not isinstance(store, EvaluationStore):
        raise RuntimeError("EvaluationStore 尚未初始化")
    return store


EvaluationStoreDependency = Annotated[EvaluationStore, Depends(get_evaluation_store)]


def create_router(clock: Clock) -> APIRouter:
    """创建使用可注入 UTC 时钟的控制面路由。"""

    router = APIRouter(
        responses={
            status.HTTP_422_UNPROCESSABLE_CONTENT: {
                "model": ApiErrorResponse,
                "description": "请求参数或领域输入校验失败",
            }
        }
    )

    @router.get("/health", response_model=HealthResponse, tags=["系统"])
    async def get_health() -> HealthResponse:
        return HealthResponse()

    @router.get(
        "/api/v1/evaluation-attempts",
        response_model=EvaluationAttemptPageResponse,
        tags=["评测"],
    )
    async def list_evaluation_attempts(
        store: EvaluationStoreDependency,
        before_sequence: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> EvaluationAttemptPageResponse:
        """只读查询真实模型尝试，不允许 HTTP 接口触发付费调用。"""

        return EvaluationAttemptPageResponse.from_page(
            await store.list_attempts(limit=limit, before_sequence=before_sequence)
        )

    @router.get(
        "/api/v1/evaluation-attempts/{attempt_id}",
        response_model=EvaluationAttemptResponse,
        responses={status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse}},
        tags=["评测"],
    )
    async def get_evaluation_attempt(
        attempt_id: UUID,
        store: EvaluationStoreDependency,
    ) -> EvaluationAttemptResponse:
        """读取一次调用的预算、Token、结果或稳定失败分类。"""

        return EvaluationAttemptResponse.from_stored(await store.get_attempt(attempt_id))

    @router.get(
        "/api/v1/evaluation-runs",
        response_model=EvaluationRunPageResponse,
        tags=["评测"],
    )
    async def list_evaluation_runs(
        store: EvaluationStoreDependency,
        before_sequence: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> EvaluationRunPageResponse:
        """读取已由离线命令持久化的评测历史，不触发新评测。"""

        return EvaluationRunPageResponse.from_page(
            await store.list_runs(limit=limit, before_sequence=before_sequence)
        )

    @router.get(
        "/api/v1/evaluation-runs/{run_id}",
        response_model=EvaluationRunResponse,
        responses={status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse}},
        tags=["评测"],
    )
    async def get_evaluation_run(
        run_id: UUID,
        store: EvaluationStoreDependency,
    ) -> EvaluationRunResponse:
        """返回数据集版本、完整逐场景结果和聚合对照。"""

        return EvaluationRunResponse.from_run(await store.get_run(run_id))

    @router.post(
        "/api/v1/alerts",
        response_model=AlertIngestionResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {
                "model": AlertIngestionResponse,
                "description": "完全重复告警，返回原接收结果",
            },
        },
        tags=["告警"],
    )
    async def ingest_alert(
        request: AlertRequest,
        response: Response,
        store: IncidentStoreDependency,
    ) -> AlertIngestionResponse:
        result = await store.ingest_alert(request.to_submission(ingested_at=clock()))
        if result.deduplicated:
            response.status_code = status.HTTP_200_OK
        return AlertIngestionResponse.from_result(result)

    @router.get(
        "/api/v1/incidents",
        response_model=IncidentPageResponse,
        tags=["事故"],
    )
    async def list_incidents(
        store: IncidentStoreDependency,
        incident_status: Annotated[IncidentStatus | None, Query(alias="status")] = None,
        before_display_number: Annotated[int | None, Query(ge=1)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> IncidentPageResponse:
        records = await store.list_incidents(
            status=incident_status,
            before_display_number=before_display_number,
            limit=limit + 1,
        )
        visible_records = records[:limit]
        has_more = len(records) > limit
        next_cursor = visible_records[-1].display_number if has_more else None
        return IncidentPageResponse(
            items=[IncidentResponse.from_stored(record) for record in visible_records],
            next_before_display_number=next_cursor,
        )

    @router.get(
        "/api/v1/incidents/{incident_id}",
        response_model=IncidentResponse,
        responses={status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse}},
        tags=["事故"],
    )
    async def get_incident(
        incident_id: UUID,
        store: IncidentStoreDependency,
    ) -> IncidentResponse:
        return IncidentResponse.from_stored(await store.get_incident(incident_id))

    @router.get(
        "/api/v1/incidents/{incident_id}/workroom",
        response_model=IncidentWorkroomResponse,
        responses={status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse}},
        tags=["事故"],
    )
    async def get_incident_workroom(
        incident_id: UUID,
        store: WorkroomStoreDependency,
    ) -> IncidentWorkroomResponse:
        """一次读取最近调查及其后续处置链路，不触发任何状态变化。"""

        return IncidentWorkroomResponse.from_snapshot(await store.get_snapshot(incident_id))

    @router.post(
        "/api/v1/incidents/{incident_id}/actions/close",
        response_model=IncidentResponse,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
        },
        tags=["事故动作"],
    )
    async def close_incident(
        incident_id: UUID,
        request: CloseIncidentRequest,
        store: IncidentStoreDependency,
    ) -> IncidentResponse:
        """仅允许工程师关闭已经确认恢复的 Incident。"""

        stored = await store.transition(
            incident_id,
            action=IncidentAction.CLOSE,
            expected_version=request.expected_version,
            occurred_at=clock(),
            actor=IncidentActor(actor_type=ActorType.HUMAN, actor_id=request.actor_id),
            reason=request.reason,
        )
        return IncidentResponse.from_stored(stored)

    @router.post(
        "/api/v1/incidents/{incident_id}/actions/start-investigation",
        response_model=StartInvestigationResponse,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
        },
        tags=["事故动作"],
    )
    async def start_investigation(
        incident_id: UUID,
        request: StartInvestigationRequest,
        store: InvestigationStoreDependency,
    ) -> StartInvestigationResponse:
        started = await store.start_investigation(
            incident_id,
            expected_version=request.expected_version,
            occurred_at=clock(),
            actor=IncidentActor(actor_type=ActorType.HUMAN, actor_id=request.actor_id),
            reason=request.reason,
            mode=request.mode,
        )
        return StartInvestigationResponse.from_started(started)

    @router.get(
        "/api/v1/investigations/{run_id}",
        response_model=InvestigationRunResponse,
        responses={status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse}},
        tags=["调查"],
    )
    async def get_investigation_run(
        run_id: UUID,
        store: InvestigationStoreDependency,
    ) -> InvestigationRunResponse:
        return InvestigationRunResponse.from_stored(await store.get_run(run_id))

    @router.get(
        "/api/v1/investigations/{run_id}/evidence-board",
        response_model=EvidenceBoardResponse,
        responses={status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse}},
        tags=["调查"],
    )
    async def get_evidence_board(
        run_id: UUID,
        store: InvestigationStoreDependency,
    ) -> EvidenceBoardResponse:
        """返回业务库中的证据事实，不直接读取 LangGraph checkpoint。"""

        return EvidenceBoardResponse.from_stored(await store.get_evidence_board(run_id))

    @router.post(
        "/api/v1/investigations/{run_id}/action-plans",
        response_model=CreateActionPlanResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {
                "model": CreateActionPlanResponse,
                "description": "相同假设版本已有计划，幂等返回原记录",
            },
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
        },
        tags=["处置计划"],
    )
    async def create_action_plan(
        run_id: UUID,
        response: Response,
        store: ActionPlanStoreDependency,
    ) -> CreateActionPlanResponse:
        """生成未授权草案；不创建审批、不执行动作、不迁移 Incident。"""

        creation = await store.create_for_investigation(run_id, created_at=clock())
        if not creation.created:
            response.status_code = status.HTTP_200_OK
        return CreateActionPlanResponse.from_creation(creation)

    @router.get(
        "/api/v1/investigations/{run_id}/action-plans/capability",
        response_model=ActionPlanCapabilityResponse,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
        },
        tags=["处置计划"],
    )
    async def get_action_plan_capability(
        run_id: UUID,
        store: ActionPlanStoreDependency,
    ) -> ActionPlanCapabilityResponse:
        """只读返回模板支持状态，不创建计划、审批或执行记录。"""

        capability = await store.get_capability(run_id)
        return ActionPlanCapabilityResponse.from_capability(run_id, capability)

    @router.get(
        "/api/v1/investigations/{run_id}/action-plans/current",
        response_model=ActionPlanResponse,
        responses={status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse}},
        tags=["处置计划"],
    )
    async def get_current_action_plan(
        run_id: UUID,
        store: ActionPlanStoreDependency,
    ) -> ActionPlanResponse:
        return ActionPlanResponse.from_stored(await store.get_current(run_id))

    @router.post(
        "/api/v1/action-plans/{plan_id}/approval-requests",
        response_model=CreateApprovalRequestResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {
                "model": CreateApprovalRequestResponse,
                "description": "计划已有审批请求，幂等返回原记录",
            },
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
        },
        tags=["审批"],
    )
    async def create_approval_request(
        plan_id: UUID,
        request: CreateApprovalRequest,
        response: Response,
        store: ApprovalRequestStoreDependency,
    ) -> CreateApprovalRequestResponse:
        """提交待审批请求；不接受批准、拒绝或执行决定。"""

        creation = await store.create_for_action_plan(
            plan_id,
            expected_incident_version=request.expected_incident_version,
            occurred_at=clock(),
            actor=IncidentActor(actor_type=ActorType.HUMAN, actor_id=request.actor_id),
            reason=request.reason,
        )
        if not creation.created:
            response.status_code = status.HTTP_200_OK
        return CreateApprovalRequestResponse.from_creation(creation)

    @router.get(
        "/api/v1/approval-requests/{request_id}",
        response_model=ApprovalRequestResponse,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
        },
        tags=["审批"],
    )
    async def get_approval_request(
        request_id: UUID,
        store: ApprovalRequestStoreDependency,
    ) -> ApprovalRequestResponse:
        return ApprovalRequestResponse.from_stored(await store.get(request_id))

    async def decide_approval_request(
        request_id: UUID,
        request: DecideApprovalRequest,
        store: ApprovalRequestStore,
        decision: ApprovalDecision,
    ) -> DecideApprovalRequestResponse:
        outcome = await store.decide(
            request_id,
            decision=decision,
            expected_incident_version=request.expected_incident_version,
            occurred_at=clock(),
            actor=IncidentActor(actor_type=ActorType.HUMAN, actor_id=request.actor_id),
            reason=request.reason,
        )
        if outcome.request.status is ApprovalRequestStatus.EXPIRED:
            raise ApprovalRequestExpired(
                request_id,
                expires_at=outcome.request.expires_at.isoformat(),
            )
        return DecideApprovalRequestResponse.from_outcome(outcome)

    @router.post(
        "/api/v1/approval-requests/{request_id}/actions/approve",
        response_model=DecideApprovalRequestResponse,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
        },
        tags=["审批"],
    )
    async def approve_request(
        request_id: UUID,
        request: DecideApprovalRequest,
        store: ApprovalRequestStoreDependency,
    ) -> DecideApprovalRequestResponse:
        """批准精确计划版本，但不调用执行器。"""

        return await decide_approval_request(
            request_id,
            request,
            store,
            ApprovalDecision.APPROVE,
        )

    @router.post(
        "/api/v1/approval-requests/{request_id}/actions/reject",
        response_model=DecideApprovalRequestResponse,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
        },
        tags=["审批"],
    )
    async def reject_request(
        request_id: UUID,
        request: DecideApprovalRequest,
        store: ApprovalRequestStoreDependency,
    ) -> DecideApprovalRequestResponse:
        """拒绝精确计划版本并返回调查状态。"""

        return await decide_approval_request(
            request_id,
            request,
            store,
            ApprovalDecision.REJECT,
        )

    @router.post(
        "/api/v1/action-steps/{step_id}/executions",
        response_model=ExecuteOperationResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {
                "model": ExecuteOperationResponse,
                "description": "幂等返回已有终态或租约恢复结果",
            },
            status.HTTP_202_ACCEPTED: {
                "model": ExecuteOperationResponse,
                "description": "同一幂等操作正在执行",
            },
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
            status.HTTP_502_BAD_GATEWAY: {"model": ApiErrorResponse},
        },
        tags=["受控执行"],
    )
    async def execute_action_step(
        step_id: UUID,
        request: ExecuteOperationRequest,
        response: Response,
        service: OperationExecutionServiceDependency,
    ) -> ExecuteOperationResponse:
        """执行固定本地动作；请求不能提供命令、URL 或任意参数。"""

        outcome = await service.execute_step(
            step_id,
            idempotency_key=request.idempotency_key,
            expected_incident_version=request.expected_incident_version,
            occurred_at=clock(),
        )
        if outcome.execution.status is ExecutionStatus.FAILED:
            if outcome.execution.failure is None:
                raise RuntimeError("failed 执行缺少失败详情")
            raise OperationExecutionFailed(
                outcome.execution.id,
                error_code=outcome.execution.failure.error_code,
                message=outcome.execution.failure.message,
            )
        if outcome.execution.status is ExecutionStatus.RUNNING:
            response.status_code = status.HTTP_202_ACCEPTED
        elif not outcome.created:
            response.status_code = status.HTTP_200_OK
        return ExecuteOperationResponse.from_outcome(outcome)

    @router.get(
        "/api/v1/operation-executions/{execution_id}",
        response_model=OperationExecutionResponse,
        responses={status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse}},
        tags=["受控执行"],
    )
    async def get_operation_execution(
        execution_id: UUID,
        service: OperationExecutionServiceDependency,
    ) -> OperationExecutionResponse:
        return OperationExecutionResponse.from_stored(await service.get(execution_id))

    @router.post(
        "/api/v1/operation-executions/{execution_id}/verification-runs",
        response_model=CreateVerificationRunResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {
                "model": CreateVerificationRunResponse,
                "description": "该成功操作已有验证运行，幂等返回原记录",
            },
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
        },
        tags=["恢复验证"],
    )
    async def create_verification_run(
        execution_id: UUID,
        request: CreateVerificationRunRequest,
        response: Response,
        service: VerificationServiceDependency,
    ) -> CreateVerificationRunResponse:
        """冻结已执行步骤的验证规则，不在请求中等待观察窗口。"""

        creation = await service.create_run(
            execution_id,
            expected_incident_version=request.expected_incident_version,
        )
        if not creation.created:
            response.status_code = status.HTTP_200_OK
        return CreateVerificationRunResponse.from_creation(creation)

    @router.get(
        "/api/v1/verification-runs/{run_id}",
        response_model=VerificationRunResponse,
        responses={status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse}},
        tags=["恢复验证"],
    )
    async def get_verification_run(
        run_id: UUID,
        service: VerificationServiceDependency,
    ) -> VerificationRunResponse:
        return VerificationRunResponse.from_stored(await service.get(run_id))

    @router.get(
        "/api/v1/verification-runs/{run_id}/work-item",
        response_model=VerificationWorkItemResponse,
        responses={status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse}},
        tags=["恢复验证"],
    )
    async def get_verification_work_item(
        run_id: UUID,
        service: VerificationServiceDependency,
    ) -> VerificationWorkItemResponse:
        """只读返回下一次采样时间、领取状态和有限错误。"""

        return VerificationWorkItemResponse.from_stored(await service.get_work_item(run_id))

    @router.post(
        "/api/v1/verification-runs/{run_id}/samples",
        response_model=RecordVerificationSampleResponse,
        status_code=status.HTTP_201_CREATED,
        responses={
            status.HTTP_200_OK: {
                "model": RecordVerificationSampleResponse,
                "description": "同一采样幂等键已经持久化，不重复调用探针",
            },
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
        },
        tags=["恢复验证"],
    )
    async def record_verification_sample(
        run_id: UUID,
        request: VerificationSampleRequest,
        response: Response,
        service: VerificationServiceDependency,
    ) -> RecordVerificationSampleResponse:
        """由服务端固定探针取得一个样本，调用方不能提交观测值。"""

        outcome = await service.sample(
            run_id,
            idempotency_key=request.idempotency_key,
        )
        if not outcome.created:
            response.status_code = status.HTTP_200_OK
        return RecordVerificationSampleResponse.from_outcome(outcome)

    @router.get(
        "/api/v1/incidents/{incident_id}/events",
        response_model=IncidentEventPageResponse,
        responses={status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse}},
        tags=["事故时间线"],
    )
    async def list_incident_events(
        incident_id: UUID,
        store: IncidentStoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> IncidentEventPageResponse:
        records = await store.list_events(
            incident_id,
            after_cursor=after_cursor,
            limit=limit + 1,
        )
        visible_records = records[:limit]
        has_more = len(records) > limit
        next_cursor = visible_records[-1].cursor if has_more else None
        return IncidentEventPageResponse(
            items=[IncidentEventResponse.from_stored(record) for record in visible_records],
            next_after_cursor=next_cursor,
        )

    @router.get(
        "/api/v1/incidents/{incident_id}/stream",
        response_class=StreamingResponse,
        responses={
            status.HTTP_200_OK: {
                "description": "支持 Last-Event-ID 断线重放的 SSE 时间线",
                "content": {"text/event-stream": {"schema": {"type": "string"}}},
            },
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
        },
        tags=["事故时间线"],
    )
    async def stream_incident_events(
        incident_id: UUID,
        request: Request,
        store: IncidentStoreDependency,
        last_event_id: Annotated[
            int | None,
            Header(alias="Last-Event-ID", ge=0),
        ] = None,
    ) -> StreamingResponse:
        """连接前确认 Incident 存在，随后只推送已经落库的事件。"""

        await store.get_incident(incident_id)
        stream = IncidentEventStream(store)
        return StreamingResponse(
            stream.stream(
                incident_id,
                after_cursor=last_event_id if last_event_id is not None else 0,
                is_disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
