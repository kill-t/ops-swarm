"""可信经验审核的 HTTP 转换；业务拒绝在结果审计提交后返回。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from opsswarm.experience.review_contracts import EntryView, ReviewCommand, ReviewResult, ReviewState
from opsswarm.experience.review_store import ExperienceReviewStore
from opsswarm.identity.models import OperatorPrincipal


def get_review_store(request: Request) -> ExperienceReviewStore:
    store = getattr(request.app.state, "experience_review_store", None)
    if not isinstance(store, ExperienceReviewStore):
        raise RuntimeError("ExperienceReviewStore 尚未初始化")
    return store


ReviewStoreDependency = Annotated[ExperienceReviewStore, Depends(get_review_store)]


def create_experience_review_router() -> APIRouter:
    router = APIRouter(tags=["经验审核"])

    @router.post(
        "/api/v1/experience-candidates/{candidate_id}/reviews",
        status_code=201,
        responses={200: {"model": ReviewResult}},
    )
    async def review_experience_candidate(
        candidate_id: UUID,
        body: ReviewCommand,
        request: Request,
        response: Response,
        store: ReviewStoreDependency,
    ) -> ReviewResult:
        outcome = await store.decide(
            candidate_id,
            body,
            principal=cast(OperatorPrincipal, request.state.operator_principal),
            authorization_id=cast(UUID, request.state.authorization_id),
        )
        if outcome.error is not None:
            raise outcome.error
        assert outcome.result is not None
        if not outcome.result.applied:
            response.status_code = 200
        return outcome.result

    @router.get("/api/v1/experience-candidates/{candidate_id}/review")
    async def get_experience_review(
        candidate_id: UUID,
        store: ReviewStoreDependency,
    ) -> ReviewState:
        return await store.get_state(candidate_id)

    @router.get("/api/v1/experience-entries/{entry_id}")
    async def get_experience_entry(entry_id: UUID, store: ReviewStoreDependency) -> EntryView:
        return await store.get_entry(entry_id)

    return router
