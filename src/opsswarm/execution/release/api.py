"""可信候选发布准备请求及审计的HTTP转换。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from opsswarm.execution.release.authorization import ROUTE
from opsswarm.execution.release.contracts import (
    ReleaseRequestAuditPage,
    ReleaseRequestCommand,
    ReleaseRequestResult,
    ReleaseRequestView,
)
from opsswarm.execution.release.store import CandidateReleaseRequestStore
from opsswarm.identity.models import OperatorPrincipal


def get_store(request: Request) -> CandidateReleaseRequestStore:
    store = getattr(request.app.state, "candidate_release_request_store", None)
    if not isinstance(store, CandidateReleaseRequestStore):
        raise RuntimeError("CandidateReleaseRequestStore 尚未初始化")
    return store


StoreDependency = Annotated[CandidateReleaseRequestStore, Depends(get_store)]
VersionPath = Annotated[str, Path(pattern=r"^[1-8]$")]


def create_candidate_release_router() -> APIRouter:
    router = APIRouter(prefix=ROUTE, tags=["候选发布执行准备"])

    @router.post("", status_code=201, responses={200: {"model": ReleaseRequestResult}})
    async def prepare_candidate_release_execution(
        run_id: UUID,
        job_id: UUID,
        version: VersionPath,
        body: ReleaseRequestCommand,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> ReleaseRequestResult:
        outcome = await store.create(
            run_id,
            job_id,
            int(version),
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
    async def list_candidate_release_commands(
        run_id: UUID,
        job_id: UUID,
        version: VersionPath,
        store: StoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ReleaseRequestAuditPage:
        return await store.list_commands(
            run_id, job_id, int(version), after_cursor=after_cursor, limit=limit
        )

    @router.get("")
    async def get_candidate_release_execution_request(
        run_id: UUID,
        job_id: UUID,
        version: VersionPath,
        store: StoreDependency,
    ) -> ReleaseRequestView:
        return await store.get(run_id, job_id, int(version))

    return router
