"""精确核验版本的独立审批请求、决定与审计HTTP转换。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Path, Query, Request, Response

from opsswarm.approval.conditions.authorization import BASE_ROUTE
from opsswarm.approval.conditions.contracts import (
    ApprovalAuditPage,
    DecisionCommand,
    DecisionResult,
    DecisionView,
    RequestCommand,
    RequestResult,
    RequestView,
)
from opsswarm.approval.conditions.store import ConditionApprovalStore
from opsswarm.identity.models import OperatorPrincipal


def get_store(request: Request) -> ConditionApprovalStore:
    store = getattr(request.app.state, "condition_approval_store", None)
    if not isinstance(store, ConditionApprovalStore):
        raise RuntimeError("ConditionApprovalStore 尚未初始化")
    return store


StoreDependency = Annotated[ConditionApprovalStore, Depends(get_store)]
VersionPath = Annotated[str, Path(pattern=r"^[1-8]$")]


def create_condition_approval_router() -> APIRouter:
    router = APIRouter(prefix=BASE_ROUTE, tags=["核验依据审批"])

    @router.post("", status_code=201, responses={200: {"model": RequestResult}})
    async def create_condition_approval_request(
        run_id: UUID,
        job_id: UUID,
        version: VersionPath,
        body: RequestCommand,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> RequestResult:
        outcome = await store.create_request(
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
        assert isinstance(outcome.result, RequestResult)
        if not outcome.result.applied:
            response.status_code = 200
        return outcome.result

    @router.post("/decision", status_code=201, responses={200: {"model": DecisionResult}})
    async def decide_condition_approval(
        run_id: UUID,
        job_id: UUID,
        version: VersionPath,
        body: DecisionCommand,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> DecisionResult:
        outcome = await store.decide(
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
        assert isinstance(outcome.result, DecisionResult)
        if not outcome.result.applied:
            response.status_code = 200
        return outcome.result

    @router.get("/commands")
    async def list_condition_approval_commands(
        run_id: UUID,
        job_id: UUID,
        version: VersionPath,
        store: StoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ApprovalAuditPage:
        return await store.list_commands(
            run_id, job_id, int(version), after_cursor=after_cursor, limit=limit
        )

    @router.get("/decision")
    async def get_condition_approval_decision(
        run_id: UUID,
        job_id: UUID,
        version: VersionPath,
        store: StoreDependency,
    ) -> DecisionView:
        return await store.get_decision(run_id, job_id, int(version))

    @router.get("")
    async def get_condition_approval_request(
        run_id: UUID,
        job_id: UUID,
        version: VersionPath,
        store: StoreDependency,
    ) -> RequestView:
        return await store.get_request(run_id, job_id, int(version))

    return router
