"""按发生窗口和入库截止读取历史变更，不查询当前运行清单。"""

from collections.abc import Callable
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query

from opsswarm.change_records.models import MAX_CHANGE_RECORDS
from opsswarm.change_records.persistence import ChangeRecordStore
from opsswarm.change_records.window_models import ChangeRecordWindowResponse, ChangeWindowQuery
from opsswarm.observation_window import ObservationWindow


def create_window_router(store: ChangeRecordStore, clock: Callable[[], datetime]) -> APIRouter:
    router = APIRouter(tags=["只读查询"])

    @router.get("/api/v1/changes/window", response_model=ChangeRecordWindowResponse)
    async def list_window(
        query: Annotated[ChangeWindowQuery, Query()],
    ) -> ChangeRecordWindowResponse:
        generated_at = clock()
        window = ObservationWindow(window_start=query.window_start, window_end=query.window_end)
        try:
            window.require_elapsed(generated_at)
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        page = await store.list_recent(
            service_name=query.service_name,
            since=window.window_start,
            until=window.window_end,
            recorded_until=window.window_end,
            limit=MAX_CHANGE_RECORDS,
        )
        return ChangeRecordWindowResponse(
            window=window, generated_at=generated_at, items=page.items, truncated=page.truncated
        )

    return router
