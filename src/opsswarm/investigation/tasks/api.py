"""专业任务事实的只读 HTTP 路由。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request

from opsswarm.control_plane.schemas import ApiErrorResponse
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.records import CollaborationSnapshot


def get_collaboration_queries(request: Request) -> CollaborationQueries:
    """查询依赖由应用生命周期提供，不在请求中创建连接池。"""

    queries = getattr(request.app.state, "collaboration_queries", None)
    if not isinstance(queries, CollaborationQueries):
        raise RuntimeError("CollaborationQueries 尚未初始化")
    return queries


def create_collaboration_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["investigation-tasks"])

    @router.get(
        "/investigations/{run_id}/collaboration",
        response_model=CollaborationSnapshot,
        responses={404: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    async def read_collaboration(
        run_id: UUID,
        queries: Annotated[CollaborationQueries, Depends(get_collaboration_queries)],
    ) -> CollaborationSnapshot:
        """返回有界任务历史；无记录的旧运行不生成推断结果。"""

        return await queries.get_snapshot(run_id)

    return router
