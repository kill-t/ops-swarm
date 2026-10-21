"""可信经验替代协议入口，身份和授权均来自系统认证。"""

from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response

from opsswarm.experience.replacement_contracts import ReplaceCommand, ReplaceResult
from opsswarm.experience.replacement_store import ExperienceReplacementStore
from opsswarm.identity.models import OperatorPrincipal


def replacement_store(request: Request) -> ExperienceReplacementStore:
    store = getattr(request.app.state, "experience_replacement_store", None)
    if not isinstance(store, ExperienceReplacementStore):
        raise RuntimeError("ExperienceReplacementStore 尚未初始化")
    return store


ReplacementDependency = Annotated[ExperienceReplacementStore, Depends(replacement_store)]


def create_experience_replacement_router() -> APIRouter:
    router = APIRouter(tags=["经验替代"])

    @router.post(
        "/api/v1/experience-entries/{entry_id}/actions/replace",
        status_code=201,
        responses={200: {"model": ReplaceResult}},
    )
    async def replace_experience_entry(
        entry_id: UUID,
        body: ReplaceCommand,
        request: Request,
        response: Response,
        store: ReplacementDependency,
    ) -> ReplaceResult:
        outcome = await store.replace(
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

    return router
