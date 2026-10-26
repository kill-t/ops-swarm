"""有限发布演练管理接口，不承担业务执行授权。"""

from typing import Annotated

from fastapi import APIRouter, Path, Request, Response

from opsswarm.fault_lab.release.contracts import (
    Key,
    ReleaseCommand,
    ReleaseError,
    ReleaseResult,
    ReleaseState,
)
from opsswarm.fault_lab.release.receipts import ReleaseReceipt
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

    @router.get("/commands/{idempotency_key}", responses={404: {"model": ReleaseReceipt}})
    async def get_simulated_release_receipt(
        idempotency_key: Annotated[Key, Path()], request: Request, response: Response
    ) -> ReleaseReceipt:
        result = await get_store(request).get_receipt(idempotency_key)
        response.headers["Cache-Control"] = "no-store"
        if result.status == "not_found":
            response.status_code = 404
        return result

    @router.put("", status_code=201, responses={200: {"model": ReleaseResult}})
    async def change_simulated_release(
        body: ReleaseCommand, request: Request, response: Response
    ) -> ReleaseResult:
        result = await get_store(request).change(body)
        if not result.applied:
            response.status_code = 200
        return result

    return router
