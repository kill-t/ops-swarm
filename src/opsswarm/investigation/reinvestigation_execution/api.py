"""专业执行的可信只读进度与完整材料查询。"""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowError
from opsswarm.investigation.reinvestigation_execution.contracts import ROUTE, ExecutionView
from opsswarm.investigation.reinvestigation_execution.queries import ExecutionQueries


def create_execution_router() -> APIRouter:
    router = APIRouter(tags=["回流专业执行"])

    @router.get(ROUTE)
    async def get_reinvestigation_execution(
        run_id: UUID, request: Request, response: Response
    ) -> ExecutionView:
        queries = getattr(request.app.state, "execution_queries", None)
        if not isinstance(queries, ExecutionQueries):
            raise ReflowError("execution_unavailable", "执行查询存储未初始化", 503)
        response.headers["Cache-Control"] = "no-store"
        return await queries.get(run_id)

    return router
