"""控制面 Incident HTTP 路由。"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from opsswarm.approval import ApprovalDecision, ApprovalRequestExpired, ApprovalRequestStatus
from opsswarm.approval.persistence import ApprovalRequestStore
from opsswarm.control_plane.schemas import (
    ActionPlanResponse,
    AlertIngestionResponse,
    AlertRequest,
    ApiErrorResponse,
    ApprovalRequestResponse,
    CreateActionPlanResponse,
    CreateApprovalRequest,
    CreateApprovalRequestResponse,
    DecideApprovalRequest,
    DecideApprovalRequestResponse,
    EvidenceBoardResponse,
    HealthResponse,
    IncidentEventPageResponse,
    IncidentEventResponse,
    IncidentPageResponse,
    IncidentResponse,
    InvestigationRunResponse,
    StartInvestigationRequest,
    StartInvestigationResponse,
)
from opsswarm.incidents import ActorType, IncidentActor, IncidentStatus
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.persistence import InvestigationStore
from opsswarm.planning.persistence import ActionPlanStore

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

    return router
