"""可信历史读取，HTTP不消费路由或调用模型。"""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from opsswarm.investigation.reinvestigation_review.contracts import ReflowReviewError
from opsswarm.investigation.reinvestigation_routing.contracts import ROUTE, ReflowRoutingView
from opsswarm.investigation.reinvestigation_routing.queries import ReflowRoutingQueries


def create_reflow_routing_router() -> APIRouter:
    router = APIRouter(prefix=ROUTE, tags=["回流审查路由"])

    @router.get("")
    async def get_reinvestigation_routing(
        run_id: UUID, request: Request, response: Response
    ) -> ReflowRoutingView:
        queries = getattr(request.app.state, "reflow_routing_queries", None)
        if not isinstance(queries, ReflowRoutingQueries):
            raise ReflowReviewError("reflow_routing_unavailable", "回流路由查询未初始化", 503)
        response.headers["Cache-Control"] = "no-store"
        return await queries.get(run_id)

    return router
