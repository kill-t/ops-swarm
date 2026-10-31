"""可信候选审批请求准入与只读追溯的HTTP转换。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from opsswarm.approval.reinvestigation_candidate.contracts import (
    ReflowRequestAuditPage,
    ReflowRequestCommand,
    ReflowRequestSubmissionResult,
    ReflowRequestView,
)
from opsswarm.approval.reinvestigation_candidate.store import ReflowApprovalStore
from opsswarm.identity.models import OperatorPrincipal


def get_store(request: Request) -> ReflowApprovalStore:
    store = getattr(request.app.state, "reflow_approval_store", None)
    if not isinstance(store, ReflowApprovalStore):
        raise RuntimeError("ReflowApprovalStore 尚未初始化")
    return store


StoreDependency = Annotated[ReflowApprovalStore, Depends(get_store)]


def create_reflow_approval_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/investigations/{run_id}/reinvestigation-candidate/action-plan/approval-request",
        tags=["候选审批请求"],
    )

    @router.post("", status_code=201, responses={200: {"model": ReflowRequestSubmissionResult}})
    async def create_reinvestigation_approval_request(
        run_id: UUID,
        body: ReflowRequestCommand,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> ReflowRequestSubmissionResult:
        response.headers["Cache-Control"] = "no-store"
        outcome = await store.create(
            run_id,
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
    async def list_reinvestigation_approval_commands(
        run_id: UUID,
        response: Response,
        store: StoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> ReflowRequestAuditPage:
        response.headers["Cache-Control"] = "no-store"
        return await store.list_commands(run_id, run_id, after_cursor=after_cursor, limit=limit)

    @router.get("")
    async def get_reinvestigation_approval_request(
        run_id: UUID, response: Response, store: StoreDependency
    ) -> ReflowRequestView:
        response.headers["Cache-Control"] = "no-store"
        return await store.get(run_id, run_id)

    return router
