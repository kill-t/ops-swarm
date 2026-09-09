"""专业任务事实的只读 HTTP 路由。"""

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from opsswarm.control_plane.schemas import ApiErrorResponse
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.events import TaskEventRecord
from opsswarm.investigation.tasks.evidence import TaskEvidenceRecord
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.records import CollaborationSnapshot
from opsswarm.investigation.tasks.results import TaskResultRecord
from opsswarm.investigation.tasks.stream import stream_task_events
from opsswarm.investigation.working_summary.contracts import WorkingSummaryRecord


def get_collaboration_queries(request: Request) -> CollaborationQueries:
    """查询依赖由应用生命周期提供，不在请求中创建连接池。"""

    queries = getattr(request.app.state, "collaboration_queries", None)
    if not isinstance(queries, CollaborationQueries):
        raise RuntimeError("CollaborationQueries 尚未初始化")
    return queries


def create_collaboration_router() -> APIRouter:
    router = APIRouter(prefix="/api/v1", tags=["investigation-tasks"])

    @router.get(
        "/investigations/{run_id}/collaboration",
        response_model=CollaborationSnapshot,
        responses={404: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    async def read_collaboration(
        run_id: UUID,
        queries: Annotated[CollaborationQueries, Depends(get_collaboration_queries)],
    ) -> CollaborationSnapshot:
        """返回有界任务历史；无记录的旧运行不生成推断结果。"""

        return await queries.get_snapshot(run_id)

    @router.get(
        "/investigations/{run_id}/collaboration/tasks/{task_id}/result",
        response_model=TaskResultRecord | None,
        responses={404: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    async def read_result(
        run_id: UUID,
        task_id: UUID,
        queries: Annotated[CollaborationQueries, Depends(get_collaboration_queries)],
    ) -> TaskResultRecord | None | JSONResponse:
        try:
            return await queries.get_result(run_id, task_id)
        except TaskFactConflict as error:
            if error.code != "task_not_found":
                raise
            return JSONResponse(
                status_code=404,
                content=ApiErrorResponse(
                    error_code=error.code,
                    message=str(error),
                ).model_dump(mode="json"),
            )

    @router.get(
        "/investigations/{run_id}/collaboration/tasks/{task_id}/working-summary",
        response_model=WorkingSummaryRecord | None,
        responses={
            404: {"model": ApiErrorResponse},
            409: {"model": ApiErrorResponse},
            422: {"model": ApiErrorResponse},
        },
    )
    async def read_working_summary(
        run_id: UUID,
        task_id: UUID,
        queries: Annotated[CollaborationQueries, Depends(get_collaboration_queries)],
    ) -> WorkingSummaryRecord | None | JSONResponse:
        try:
            return await queries.get_working_summary(run_id, task_id)
        except TaskFactConflict as error:
            return JSONResponse(
                status_code=404 if error.code == "task_not_found" else 409,
                content=ApiErrorResponse(error_code=error.code, message=str(error)).model_dump(
                    mode="json"
                ),
            )

    @router.get(
        "/investigations/{run_id}/collaboration/tasks/{task_id}/evidence",
        response_model=TaskEvidenceRecord | None,
        responses={404: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    async def read_evidence(
        run_id: UUID,
        task_id: UUID,
        queries: Annotated[CollaborationQueries, Depends(get_collaboration_queries)],
    ) -> TaskEvidenceRecord | None | JSONResponse:
        try:
            return await queries.get_evidence(run_id, task_id)
        except TaskFactConflict as error:
            if error.code != "task_not_found":
                raise
            return JSONResponse(
                status_code=404,
                content=ApiErrorResponse(error_code=error.code, message=str(error)).model_dump(
                    mode="json"
                ),
            )

    @router.get(
        "/investigations/{run_id}/collaboration/events",
        response_model=tuple[TaskEventRecord, ...],
        responses={404: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    async def read_events(
        run_id: UUID,
        queries: Annotated[CollaborationQueries, Depends(get_collaboration_queries)],
        after_cursor: Annotated[int, Query(ge=0)] = 0,
        limit: Annotated[int, Query(ge=1, le=100)] = 100,
    ) -> tuple[TaskEventRecord, ...]:
        return await queries.list_events(run_id, after_cursor=after_cursor, limit=limit)

    @router.get(
        "/investigations/{run_id}/collaboration/events/stream",
        response_class=StreamingResponse,
        responses={
            200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}},
            404: {"model": ApiErrorResponse},
            422: {"model": ApiErrorResponse},
        },
    )
    async def stream_events(
        run_id: UUID,
        request: Request,
        queries: Annotated[CollaborationQueries, Depends(get_collaboration_queries)],
        last_event_id: Annotated[int, Header(alias="Last-Event-ID", ge=0)] = 0,
    ) -> StreamingResponse:
        await queries.list_events(run_id, limit=1)
        return StreamingResponse(
            stream_task_events(
                queries,
                run_id,
                after_cursor=last_event_id,
                is_disconnected=request.is_disconnected,
            ),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    return router
