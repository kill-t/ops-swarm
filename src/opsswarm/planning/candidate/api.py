"""可信候选草案准入与只读追溯的HTTP转换。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from opsswarm.identity.models import OperatorPrincipal
from opsswarm.planning.candidate.contracts import (
    PlanAuditPage,
    PlanCommand,
    PlanSubmissionResult,
    PlanView,
)
from opsswarm.planning.candidate.store import CandidatePlanStore


def get_store(request: Request) -> CandidatePlanStore:
    store = getattr(request.app.state, "candidate_plan_store", None)
    if not isinstance(store, CandidatePlanStore):
        raise RuntimeError("CandidatePlanStore 尚未初始化")
    return store


StoreDependency = Annotated[CandidatePlanStore, Depends(get_store)]


def create_candidate_plan_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan",
        tags=["候选计划"],
    )

    @router.post("", status_code=201, responses={200: {"model": PlanSubmissionResult}})
    async def create_candidate_action_plan(
        run_id: UUID,
        job_id: UUID,
        body: PlanCommand,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> PlanSubmissionResult:
        outcome = await store.create(
            run_id,
            job_id,
            body,
            principal=cast(OperatorPrincipal, request.state.operator_principal),
            authorization_id=cast(UUID, request.state.authorization_id),
            raw_body=await request.body(),
        )
        if outcome.error is not None:
            raise outcome.error
        assert outcome.result is not None
        if not outcome.result.applied:
            response.status_code = 200
        return outcome.result

    @router.get("/commands")
    async def list_candidate_plan_commands(
        run_id: UUID,
        job_id: UUID,
        store: StoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> PlanAuditPage:
        return await store.list_commands(run_id, job_id, after_cursor=after_cursor, limit=limit)

    @router.get("")
    async def get_candidate_action_plan(
        run_id: UUID, job_id: UUID, store: StoreDependency
    ) -> PlanView:
        return await store.get(run_id, job_id)

    return router
