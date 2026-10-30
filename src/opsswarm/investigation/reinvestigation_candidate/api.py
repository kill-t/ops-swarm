"""只读回流阶段及完整冻结输入，HTTP不执行模型。"""

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Request, Response

from opsswarm.investigation.reinvestigation_candidate.contracts import (
    ROUTE,
    ReflowCandidateError,
    ReflowCandidateView,
)
from opsswarm.investigation.reinvestigation_candidate.queries import ReflowCandidateQueries


def create_reflow_candidate_router() -> APIRouter:
    router = APIRouter(prefix=ROUTE, tags=["回流候选评估阶段"])

    @router.get("")
    async def get_reinvestigation_candidate(
        run_id: UUID, request: Request, response: Response
    ) -> ReflowCandidateView:
        store = getattr(request.app.state, "reflow_candidate_queries", None)
        if not isinstance(store, ReflowCandidateQueries):
            raise ReflowCandidateError("reflow_candidate_unavailable", "回流查询存储未初始化", 503)
        response.headers["Cache-Control"] = "no-store"
        return await store.get(run_id)

    @router.get("/input")
    async def get_reinvestigation_candidate_input(
        run_id: UUID, request: Request, response: Response
    ) -> dict[str, Any]:
        view = await get_reinvestigation_candidate(run_id, request, response)
        return {
            "job_id": str(view.job_id) if view.job_id else None,
            "packet": view.packet.model_dump(mode="json") if view.packet else None,
        }

    return router
