"""可信候选审批请求准入与只读追溯的HTTP转换。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from opsswarm.approval.candidate.contracts import (
    RequestAuditPage,
    RequestCommand,
    RequestSubmissionResult,
    RequestView,
)
from opsswarm.approval.candidate.store import CandidateApprovalStore
from opsswarm.identity.models import OperatorPrincipal


def get_store(request: Request) -> CandidateApprovalStore:
    store = getattr(request.app.state, "candidate_approval_store", None)
    if not isinstance(store, CandidateApprovalStore):
        raise RuntimeError("CandidateApprovalStore 尚未初始化")
    return store


StoreDependency = Annotated[CandidateApprovalStore, Depends(get_store)]


def create_candidate_approval_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/action-plan/approval-request",
        tags=["候选审批请求"],
    )

    @router.post("", status_code=201, responses={200: {"model": RequestSubmissionResult}})
    async def create_candidate_approval_request(
        run_id: UUID,
        job_id: UUID,
        body: RequestCommand,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> RequestSubmissionResult:
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
    async def list_candidate_approval_commands(
        run_id: UUID,
        job_id: UUID,
        store: StoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> RequestAuditPage:
        return await store.list_commands(run_id, job_id, after_cursor=after_cursor, limit=limit)

    @router.get("")
    async def get_candidate_approval_request(
        run_id: UUID, job_id: UUID, store: StoreDependency
    ) -> RequestView:
        return await store.get(run_id, job_id)

    return router
