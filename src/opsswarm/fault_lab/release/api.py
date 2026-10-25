"""有限发布演练管理接口，不承担业务执行授权。"""

from fastapi import APIRouter, Request, Response

from opsswarm.fault_lab.release.contracts import (
    ReleaseCommand,
    ReleaseError,
    ReleaseResult,
    ReleaseState,
)
from opsswarm.fault_lab.release.store import LabReleaseStore


def get_store(request: Request) -> LabReleaseStore:
    store = getattr(request.app.state, "lab_release_store", None)
    if not isinstance(store, LabReleaseStore):
        raise ReleaseError("lab_release_disabled", "持久发布演练模式未启用", 503)
    return store


def create_release_router() -> APIRouter:
    router = APIRouter(prefix="/__admin/simulated-release", tags=["发布演练"])

    @router.get("")
    async def get_simulated_release(request: Request) -> ReleaseState:
        return await get_store(request).get()

    @router.put("", status_code=201, responses={200: {"model": ReleaseResult}})
    async def change_simulated_release(
        body: ReleaseCommand, request: Request, response: Response
    ) -> ReleaseResult:
        result = await get_store(request).change(body)
        if not result.applied:
            response.status_code = 200
        return result

    return router
