"""可信执行任务创建及历史查询的HTTP协议转换。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request, Response

from opsswarm.execution.release_jobs.contracts import (
    ROUTE,
    ExecutionAuditPage,
    ExecutionCommand,
    ExecutionJobError,
    ExecutionJobView,
    ExecutionSubmission,
)
from opsswarm.execution.release_jobs.store import ReleaseExecutionStore
from opsswarm.identity.models import OperatorPrincipal

VersionPath = Annotated[str, Path(pattern=r"^[1-8]$")]


def get_store(request: Request) -> ReleaseExecutionStore:
    store = getattr(request.app.state, "release_execution_store", None)
    if not isinstance(store, ReleaseExecutionStore):
        raise ExecutionJobError("release_execution_unavailable", "执行任务存储未初始化", 503)
    return store


def create_release_execution_router() -> APIRouter:
    router = APIRouter(prefix=ROUTE, tags=["候选发布执行任务"])

    @router.post("", status_code=201, responses={200: {"model": ExecutionSubmission}})
    async def create_candidate_release_execution_job(
        run_id: UUID,
        job_id: UUID,
        version: VersionPath,
        body: ExecutionCommand,
        request: Request,
        response: Response,
    ) -> ExecutionSubmission:
        outcome = await get_store(request).create(
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
        assert outcome.submission is not None
        if not outcome.submission.applied:
            response.status_code = 200
        response.headers["Cache-Control"] = "no-store"
        return outcome.submission

    @router.get("")
    async def get_candidate_release_execution_job(
        run_id: UUID, job_id: UUID, version: VersionPath, request: Request
    ) -> ExecutionJobView:
        return await get_store(request).get(run_id, job_id, int(version))

    @router.get("/commands")
    async def list_candidate_release_execution_job_commands(
        run_id: UUID,
        job_id: UUID,
        version: VersionPath,
        request: Request,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ExecutionAuditPage:
        return await get_store(request).list_commands(
            run_id, job_id, int(version), after_cursor=after_cursor, limit=limit
        )

    return router
