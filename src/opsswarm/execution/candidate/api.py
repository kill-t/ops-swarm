"""可信执行前检查及不可变历史的 HTTP 协议转换。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from opsswarm.execution.candidate.contracts import (
    PreflightAuditPage,
    PreflightCommand,
    PreflightPage,
    PreflightResult,
)
from opsswarm.execution.candidate.store import CandidatePreflightStore
from opsswarm.identity.models import OperatorPrincipal


def get_store(request: Request) -> CandidatePreflightStore:
    store = getattr(request.app.state, "candidate_preflight_store", None)
    if not isinstance(store, CandidatePreflightStore):
        raise RuntimeError("CandidatePreflightStore 尚未初始化")
    return store


StoreDependency = Annotated[CandidatePreflightStore, Depends(get_store)]


def create_candidate_preflight_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-preflights",
        tags=["候选执行前检查"],
    )

    @router.post("", status_code=201, responses={200: {"model": PreflightResult}})
    async def check_candidate_execution(
        run_id: UUID,
        job_id: UUID,
        body: PreflightCommand,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> PreflightResult:
        submission = await store.create(
            run_id,
            job_id,
            body,
            principal=cast(OperatorPrincipal, request.state.operator_principal),
            authorization_id=cast(UUID, request.state.authorization_id),
            raw_body=await request.body(),
        )
        if submission.error is not None:
            raise submission.error
        assert submission.result is not None
        if not submission.result.applied:
            response.status_code = 200
        return submission.result

    @router.get("/commands")
    async def list_candidate_preflight_commands(
        run_id: UUID,
        job_id: UUID,
        store: StoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> PreflightAuditPage:
        return await store.list_commands(run_id, job_id, after_cursor=after_cursor, limit=limit)

    @router.get("")
    async def list_candidate_execution_preflights(
        run_id: UUID,
        job_id: UUID,
        store: StoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> PreflightPage:
        return await store.get(run_id, job_id, after_cursor=after_cursor, limit=limit)

    return router
