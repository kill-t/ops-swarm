"""将可信协作台的只读导航暴露给事故详情。"""

from uuid import UUID

from fastapi import APIRouter, HTTPException, Request, Response

from opsswarm.database import Database
from opsswarm.investigation.collaboration_navigation import (
    CollaborationNavigation,
    read_collaboration_navigation,
)


def create_collaboration_navigation_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/incidents/{incident_id}/collaboration-navigation")
    async def get_collaboration_navigation(
        incident_id: UUID, request: Request, response: Response
    ) -> CollaborationNavigation | None:
        response.headers["Cache-Control"] = "no-store"
        database = getattr(request.app.state, "database", None)
        if not isinstance(database, Database):
            raise HTTPException(status_code=503, detail="协作台数据源尚未初始化")
        return await read_collaboration_navigation(database.session_factory, incident_id)

    return router
