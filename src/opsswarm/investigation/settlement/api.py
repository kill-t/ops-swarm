"""查询已保存的候选收尾，不在HTTP请求中推进父流程。"""

from uuid import UUID

from fastapi import APIRouter, Request, Response

from opsswarm.investigation.settlement.contracts import ROUTE, SettlementError, SettlementView
from opsswarm.investigation.settlement.store import CandidateSettlementStore


def create_candidate_settlement_router() -> APIRouter:
    router = APIRouter(prefix=ROUTE, tags=["候选父运行收尾"])

    @router.get("")
    async def get_candidate_settlement(
        run_id: UUID, request: Request, response: Response
    ) -> SettlementView:
        store = getattr(request.app.state, "candidate_settlement_store", None)
        if not isinstance(store, CandidateSettlementStore):
            raise SettlementError("candidate_settlement_unavailable", "候选收尾存储未初始化", 503)
        response.headers["Cache-Control"] = "no-store"
        return await store.get(run_id)

    return router
