"""隔离演练管理端的远端操作生命周期协议转换。"""

from uuid import UUID

from fastapi import APIRouter, Body, Request, Response

from opsswarm.fault_lab.release.contracts import ReleaseError
from opsswarm.fault_lab.release.dispatch_contracts import (
    DispatchCommand,
    DispatchResult,
    DispatchView,
)
from opsswarm.fault_lab.release.dispatch_store import LabReleaseDispatchStore


def get_dispatch_store(request: Request) -> LabReleaseDispatchStore:
    store = getattr(request.app.state, "lab_release_dispatch_store", None)
    if not isinstance(store, LabReleaseDispatchStore):
        raise ReleaseError("lab_release_disabled", "持久发布演练模式未启用", 503)
    return store


def create_dispatch_router() -> APIRouter:
    router = APIRouter(prefix="/__admin/simulated-release/dispatches", tags=["发布演练派发"])

    @router.post("", status_code=201, responses={200: {"model": DispatchResult}})
    async def register_dispatch(
        body: DispatchCommand, request: Request, response: Response
    ) -> DispatchResult:
        result = await get_dispatch_store(request).register(body)
        response.headers["Cache-Control"] = "no-store"
        if not result.applied:
            response.status_code = 200
        return result

    @router.get("/{operation_id}")
    async def get_dispatch(
        operation_id: UUID, request: Request, response: Response
    ) -> DispatchView:
        response.headers["Cache-Control"] = "no-store"
        return await get_dispatch_store(request).get(operation_id)

    @router.put("/{operation_id}", status_code=201, responses={200: {"model": DispatchResult}})
    async def execute_dispatch(
        operation_id: UUID, request: Request, response: Response, body: None = Body(default=None)
    ) -> DispatchResult:
        result = await get_dispatch_store(request).execute(operation_id)
        response.headers["Cache-Control"] = "no-store"
        if not result.applied:
            response.status_code = 200
        return result

    @router.delete("/{operation_id}")
    async def cancel_dispatch(
        operation_id: UUID, request: Request, response: Response, body: None = Body(default=None)
    ) -> DispatchResult:
        response.headers["Cache-Control"] = "no-store"
        return await get_dispatch_store(request).cancel(operation_id)

    return router
