"""只读事故收敛查询，HTTP不执行采样或状态迁移。"""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from opsswarm.incidents.convergence.contracts import ROUTE, ConvergenceError, ConvergenceView
from opsswarm.incidents.convergence.store import CandidateIncidentConvergenceStore


def create_candidate_incident_convergence_router() -> APIRouter:
    router = APIRouter(prefix=ROUTE, tags=["候选事故收敛"])

    @router.get("")
    async def get_candidate_incident_convergence(
        run_id: UUID, request: Request, response: Response
    ) -> ConvergenceView:
        store = getattr(request.app.state, "candidate_incident_convergence_store", None)
        if not isinstance(store, CandidateIncidentConvergenceStore):
            raise ConvergenceError("candidate_convergence_unavailable", "事故收敛存储未初始化", 503)
        response.headers["Cache-Control"] = "no-store"
        return await store.get(run_id)

    return router
