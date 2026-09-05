"""共享调用预算及预留记录的只读路由。"""

from uuid import UUID

from fastapi import APIRouter, Request

from opsswarm.control_plane.schemas import ApiErrorResponse
from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.queries import CallBudgetQueries


def create_call_budget_router() -> APIRouter:
    router = APIRouter(tags=["investigation-budget"])

    @router.get(
        "/api/v1/investigations/{run_id}/call-budget",
        response_model=CallBudgetSnapshot,
        responses={404: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    async def read_budget(run_id: UUID, request: Request) -> CallBudgetSnapshot:
        queries = getattr(request.app.state, "call_budget_queries", None)
        if not isinstance(queries, CallBudgetQueries):
            raise RuntimeError("CallBudgetQueries 尚未初始化")
        return await queries.get_snapshot(run_id)

    return router
