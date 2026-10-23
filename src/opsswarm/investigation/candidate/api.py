"""可信重评命令、完整工件及业务审计的 HTTP 转换。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from opsswarm.identity.models import OperatorPrincipal
from opsswarm.investigation.candidate.contracts import (
    CandidateAuditPage,
    CandidateJobView,
    CandidateSubmissionResult,
    EvaluateCandidateCommand,
)
from opsswarm.investigation.candidate.store import CandidateStore


def get_store(request: Request) -> CandidateStore:
    store = getattr(request.app.state, "candidate_store", None)
    if not isinstance(store, CandidateStore):
        raise RuntimeError("CandidateStore 尚未初始化")
    return store


StoreDependency = Annotated[CandidateStore, Depends(get_store)]


def create_candidate_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/investigations/{run_id}/candidate-evaluations",
        tags=["候选评估"],
    )

    @router.post("", status_code=201, responses={200: {"model": CandidateSubmissionResult}})
    async def evaluate_investigation_candidate(
        run_id: UUID,
        body: EvaluateCandidateCommand,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> CandidateSubmissionResult:
        outcome = await store.create(
            run_id,
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
    async def list_investigation_candidate_commands(
        run_id: UUID,
        store: StoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> CandidateAuditPage:
        return await store.list_commands(run_id, after_cursor=after_cursor, limit=limit)

    @router.get("/{job_id}")
    async def get_investigation_candidate(
        run_id: UUID, job_id: UUID, store: StoreDependency
    ) -> CandidateJobView:
        return await store.get(run_id, job_id)

    return router
