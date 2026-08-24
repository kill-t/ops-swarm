"""控制面 Incident HTTP 路由。"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response, status

from opsswarm.control_plane.schemas import (
    AlertIngestionResponse,
    AlertRequest,
    ApiErrorResponse,
    HealthResponse,
    IncidentEventPageResponse,
    IncidentEventResponse,
    IncidentPageResponse,
    IncidentResponse,
    StartInvestigationRequest,
)
from opsswarm.incidents import ActorType, IncidentAction, IncidentActor, IncidentStatus
from opsswarm.incidents.persistence import IncidentStore

Clock = Callable[[], datetime]


def get_incident_store(request: Request) -> IncidentStore:
    """从应用生命周期状态获取仓储。"""

    store = getattr(request.app.state, "incident_store", None)
    if not isinstance(store, IncidentStore):
        raise RuntimeError("IncidentStore 尚未初始化")
    return store


IncidentStoreDependency = Annotated[IncidentStore, Depends(get_incident_store)]


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
        response_model=IncidentResponse,
        responses={
            status.HTTP_404_NOT_FOUND: {"model": ApiErrorResponse},
            status.HTTP_409_CONFLICT: {"model": ApiErrorResponse},
        },
        tags=["事故动作"],
    )
    async def start_investigation(
        incident_id: UUID,
        request: StartInvestigationRequest,
        store: IncidentStoreDependency,
    ) -> IncidentResponse:
        stored = await store.transition(
            incident_id,
            action=IncidentAction.START_INVESTIGATION,
            expected_version=request.expected_version,
            occurred_at=clock(),
            actor=IncidentActor(actor_type=ActorType.HUMAN, actor_id=request.actor_id),
            reason=request.reason,
        )
        return IncidentResponse.from_stored(stored)

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
