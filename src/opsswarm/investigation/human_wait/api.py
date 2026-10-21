"""人工等待的可信 HTTP 命令和有界只读查询。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from opsswarm.identity.models import OperatorPrincipal
from opsswarm.investigation.human_wait.contracts import (
    CancelWaitCommand,
    HumanWaitAuditPage,
    HumanWaitResult,
    HumanWaitState,
    SupplementWaitCommand,
)
from opsswarm.investigation.human_wait.store import HumanWaitOutcome, HumanWaitStore


def get_human_wait_store(request: Request) -> HumanWaitStore:
    store = getattr(request.app.state, "human_wait_store", None)
    if not isinstance(store, HumanWaitStore):
        raise RuntimeError("HumanWaitStore 尚未初始化")
    return store


WaitStoreDependency = Annotated[HumanWaitStore, Depends(get_human_wait_store)]


def _response(outcome: HumanWaitOutcome, response: Response) -> HumanWaitResult:
    if outcome.error is not None:
        raise outcome.error
    assert outcome.result is not None
    if not outcome.result.applied:
        response.status_code = 200
    return outcome.result


def create_human_wait_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1/investigations/{run_id}/human-wait", tags=["人工等待"])

    @router.get("")
    async def get_investigation_wait(run_id: UUID, store: WaitStoreDependency) -> HumanWaitState:
        return await store.get_state(run_id)

    @router.get("/commands")
    async def list_investigation_wait_commands(
        run_id: UUID,
        store: WaitStoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> HumanWaitAuditPage:
        return await store.list_commands(run_id, after_cursor=after_cursor, limit=limit)

    @router.post("/supplements", status_code=201, responses={200: {"model": HumanWaitResult}})
    async def supplement_investigation_wait(
        run_id: UUID,
        body: SupplementWaitCommand,
        request: Request,
        response: Response,
        store: WaitStoreDependency,
    ) -> HumanWaitResult:
        outcome = await store.submit(
            run_id,
            body,
            principal=cast(OperatorPrincipal, request.state.operator_principal),
            authorization_id=cast(UUID, request.state.authorization_id),
            raw_body=await request.body(),
        )
        return _response(outcome, response)

    @router.post("/cancel", status_code=201, responses={200: {"model": HumanWaitResult}})
    async def cancel_investigation_wait(
        run_id: UUID,
        body: CancelWaitCommand,
        request: Request,
        response: Response,
        store: WaitStoreDependency,
    ) -> HumanWaitResult:
        outcome = await store.submit(
            run_id,
            body,
            principal=cast(OperatorPrincipal, request.state.operator_principal),
            authorization_id=cast(UUID, request.state.authorization_id),
            raw_body=await request.body(),
        )
        return _response(outcome, response)

    return router
