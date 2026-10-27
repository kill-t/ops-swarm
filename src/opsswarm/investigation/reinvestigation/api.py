"""回流准入的可信HTTP命令与只读历史转换。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from opsswarm.identity.models import OperatorPrincipal
from opsswarm.investigation.reinvestigation.contracts import (
    ROUTE,
    ReinvestigationAuditPage,
    ReinvestigationCommand,
    ReinvestigationError,
    ReinvestigationSubmission,
    ReinvestigationView,
)
from opsswarm.investigation.reinvestigation.store import CandidateReinvestigationStore


def get_store(request: Request) -> CandidateReinvestigationStore:
    store = getattr(request.app.state, "candidate_reinvestigation_store", None)
    if not isinstance(store, CandidateReinvestigationStore):
        raise ReinvestigationError("reinvestigation_unavailable", "回流准入存储未初始化", 503)
    return store


StoreDependency = Annotated[CandidateReinvestigationStore, Depends(get_store)]


def create_candidate_reinvestigation_router() -> APIRouter:
    router = APIRouter(prefix=ROUTE, tags=["验证失败回流准入"])

    @router.post("", status_code=201, responses={200: {"model": ReinvestigationSubmission}})
    async def request_candidate_reinvestigation(
        run_id: UUID,
        body: ReinvestigationCommand,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> ReinvestigationSubmission:
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
        response.headers["Cache-Control"] = "no-store"
        if not outcome.result.applied:
            response.status_code = 200
        return outcome.result

    @router.get("")
    async def get_candidate_reinvestigation_admission(
        run_id: UUID, response: Response, store: StoreDependency
    ) -> ReinvestigationView:
        response.headers["Cache-Control"] = "no-store"
        return await store.get(run_id)

    @router.get("/commands")
    async def list_candidate_reinvestigation_commands(
        run_id: UUID,
        response: Response,
        store: StoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ReinvestigationAuditPage:
        response.headers["Cache-Control"] = "no-store"
        return await store.list_commands(run_id, after_cursor=after_cursor, limit=limit)

    return router
