"""可信重评命令、完整工件及业务审计的 HTTP 转换。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from opsswarm.identity.models import OperatorPrincipal
from opsswarm.investigation.human_review.contracts import (
    HumanReviewAuditPage,
    HumanReviewJobView,
    HumanReviewSubmissionResult,
    ReassessCommand,
)
from opsswarm.investigation.human_review.store import HumanReviewStore
from opsswarm.investigation.human_routing.contracts import HumanRoutingView


def get_store(request: Request) -> HumanReviewStore:
    store = getattr(request.app.state, "human_review_store", None)
    if not isinstance(store, HumanReviewStore):
        raise RuntimeError("HumanReviewStore 尚未初始化")
    return store


StoreDependency = Annotated[HumanReviewStore, Depends(get_store)]


def create_human_review_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/investigations/{run_id}/human-wait/preparations/{preparation_id}/reassessments",
        tags=["人工输入重评"],
    )

    @router.post("", status_code=201, responses={200: {"model": HumanReviewSubmissionResult}})
    async def reassess_investigation_human_input(
        run_id: UUID,
        preparation_id: UUID,
        body: ReassessCommand,
        request: Request,
        response: Response,
        store: StoreDependency,
    ) -> HumanReviewSubmissionResult:
        outcome = await store.create(
            run_id,
            preparation_id,
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
    async def list_investigation_human_review_commands(
        run_id: UUID,
        preparation_id: UUID,
        store: StoreDependency,
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
    ) -> HumanReviewAuditPage:
        return await store.list_commands(
            run_id, preparation_id, after_cursor=after_cursor, limit=limit
        )

    @router.get("/{job_id}")
    async def get_investigation_human_review(
        run_id: UUID, preparation_id: UUID, job_id: UUID, store: StoreDependency
    ) -> HumanReviewJobView:
        return await store.get(run_id, preparation_id, job_id)

    @router.get("/{job_id}/route")
    async def get_investigation_human_review_route(
        run_id: UUID, preparation_id: UUID, job_id: UUID, store: StoreDependency
    ) -> HumanRoutingView:
        return await store.get_route(run_id, preparation_id, job_id)

    return router
