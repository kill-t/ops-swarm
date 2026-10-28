"""可信只读分派事实查询，HTTP不推进任务或调用模型。"""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_dispatch.contracts import ROUTE, DispatchView
from opsswarm.investigation.reinvestigation_dispatch.queries import DispatchQueries


def create_dispatch_router() -> APIRouter:
    router = APIRouter(tags=["回流专业任务分派"])

    @router.get(ROUTE)
    async def get_reinvestigation_dispatch(
        run_id: UUID, request: Request, response: Response
    ) -> DispatchView:
        queries = getattr(request.app.state, "dispatch_queries", None)
        if not isinstance(queries, DispatchQueries):
            raise ReflowError("dispatch_unavailable", "分派查询存储未初始化", 503)
        response.headers["Cache-Control"] = "no-store"
        return await queries.get(run_id)

    return router
