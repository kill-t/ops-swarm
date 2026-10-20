"""当前有效性、可信停用与调查范围检索的协议入口。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Query, Request, Response

from opsswarm.experience.lifecycle_contracts import (
    DisableCommand,
    DisableResult,
    EntryState,
    ExperienceSearchQuery,
    ExperienceSearchResult,
)
from opsswarm.experience.lifecycle_store import ExperienceLifecycleStore
from opsswarm.experience.search_store import ExperienceSearchStore
from opsswarm.identity.models import OperatorPrincipal


def lifecycle_store(request: Request) -> ExperienceLifecycleStore:
    store = getattr(request.app.state, "experience_lifecycle_store", None)
    if not isinstance(store, ExperienceLifecycleStore):
        raise RuntimeError("ExperienceLifecycleStore 尚未初始化")
    return store


def search_store(request: Request) -> ExperienceSearchStore:
    store = getattr(request.app.state, "experience_search_store", None)
    if not isinstance(store, ExperienceSearchStore):
        raise RuntimeError("ExperienceSearchStore 尚未初始化")
    return store


LifecycleDependency = Annotated[ExperienceLifecycleStore, Depends(lifecycle_store)]
SearchDependency = Annotated[ExperienceSearchStore, Depends(search_store)]


def create_experience_lifecycle_router() -> APIRouter:
    router = APIRouter(tags=["有效经验"])

    @router.post(
        "/api/v1/experience-entries/{entry_id}/actions/disable",
        status_code=201,
        responses={200: {"model": DisableResult}},
    )
    async def disable_experience_entry(
        entry_id: UUID,
        body: DisableCommand,
        request: Request,
        response: Response,
        store: LifecycleDependency,
    ) -> DisableResult:
        outcome = await store.disable(
            entry_id,
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

    @router.get("/api/v1/experience-entries/{entry_id}/state")
    async def get_experience_entry_state(entry_id: UUID, store: LifecycleDependency) -> EntryState:
        return await store.get_state(entry_id)

    @router.get("/api/v1/investigations/{run_id}/experience-entries")
    async def search_experience_entries(
        run_id: UUID,
        query: Annotated[ExperienceSearchQuery, Query()],
        store: SearchDependency,
    ) -> ExperienceSearchResult:
        return await store.search(run_id, query)

    return router
