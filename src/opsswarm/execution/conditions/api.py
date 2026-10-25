"""可信执行条件核验及不可变历史的 HTTP 协议转换。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from opsswarm.execution.conditions.contracts import (
    ConditionAuditPage,
    ConditionCommand,
    ConditionResult,
    ConditionState,
    ConditionVersionView,
)
from opsswarm.execution.conditions.store import CandidateConditionStore
from opsswarm.identity.models import OperatorPrincipal


def get_store(request: Request) -> CandidateConditionStore:
    store = getattr(request.app.state, "candidate_condition_store", None)
    if not isinstance(store, CandidateConditionStore):
        raise RuntimeError("CandidateConditionStore 尚未初始化")
    return store


StoreDependency = Annotated[CandidateConditionStore, Depends(get_store)]


def create_candidate_condition_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-conditions",
        tags=["候选执行条件核验"],
    )

    @router.post("", status_code=201, responses={200: {"model": ConditionResult}})
    async def verify_candidate_conditions(
        run_id: UUID,
        job_id: UUID,
        body: ConditionCommand,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> ConditionResult:
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
    async def list_candidate_condition_commands(
        run_id: UUID,
        job_id: UUID,
        store: StoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ConditionAuditPage:
        return await store.list_commands(run_id, job_id, after_cursor=after_cursor, limit=limit)

    @router.get("/versions/{version}")
    async def get_candidate_condition_version(
        run_id: UUID,
        job_id: UUID,
        version: Annotated[int, Path(ge=1, le=8)],
        store: StoreDependency,
    ) -> ConditionVersionView:
        return await store.get_version(run_id, job_id, version)

    @router.get("")
    async def get_candidate_conditions(
        run_id: UUID,
        job_id: UUID,
        store: StoreDependency,
    ) -> ConditionState:
        return await store.get_state(run_id, job_id)

    return router
