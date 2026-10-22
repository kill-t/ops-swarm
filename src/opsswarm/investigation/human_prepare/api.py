"""可信准备命令、完整工件及业务审计的 HTTP 转换。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from opsswarm.identity.models import OperatorPrincipal
from opsswarm.investigation.human_prepare.contracts import (
    PreparationAuditPage,
    PreparationJobView,
    PreparationResult,
    PrepareCommand,
)
from opsswarm.investigation.human_prepare.store import HumanPreparationStore


def get_store(request: Request) -> HumanPreparationStore:
    store = getattr(request.app.state, "human_preparation_store", None)
    if not isinstance(store, HumanPreparationStore):
        raise RuntimeError("HumanPreparationStore 尚未初始化")
    return store


StoreDependency = Annotated[HumanPreparationStore, Depends(get_store)]


def create_human_preparation_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/investigations/{run_id}/human-wait/preparations", tags=["人工输入准备"]
    )

    @router.post("", status_code=201, responses={200: {"model": PreparationResult}})
    async def prepare_investigation_human_input(
        run_id: UUID,
        body: PrepareCommand,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> PreparationResult:
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
    async def list_investigation_human_preparation_commands(
        run_id: UUID,
        store: StoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> PreparationAuditPage:
        return await store.list_commands(run_id, after_cursor=after_cursor, limit=limit)

    @router.get("/{job_id}")
    async def get_investigation_human_preparation(
        run_id: UUID, job_id: UUID, store: StoreDependency
    ) -> PreparationJobView:
        return await store.get(run_id, job_id)

    return router
