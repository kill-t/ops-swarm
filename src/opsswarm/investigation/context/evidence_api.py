"""上下文证据目录与详情的只读 HTTP 协议转换。"""

from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, Path, Query, Request
from fastapi.responses import JSONResponse

from opsswarm.control_plane.schemas import ApiErrorResponse
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.context.evidence_contracts import (
    ContextEvidenceDetail,
    EvidenceCatalog,
    EvidenceReadRequest,
)
from opsswarm.investigation.context.evidence_reader import ContextEvidenceReader
from opsswarm.investigation.tasks.errors import TaskFactConflict
from opsswarm.investigation.tasks.queries import CollaborationQueries


def _reader(request: Request, run_id: UUID, call_id: UUID) -> ContextEvidenceReader:
    calls = getattr(request.app.state, "call_budget_queries", None)
    tasks = getattr(request.app.state, "collaboration_queries", None)
    if not isinstance(calls, CallBudgetQueries) or not isinstance(tasks, CollaborationQueries):
        raise RuntimeError("上下文证据查询依赖尚未初始化")
    return ContextEvidenceReader(run_id=run_id, call_id=call_id, calls=calls, tasks=tasks)


def _error(error: TaskFactConflict) -> JSONResponse:
    status = (
        404 if error.code in ("context_manifest_not_found", "context_evidence_not_found") else 409
    )
    return JSONResponse(
        status_code=status,
        content=ApiErrorResponse(
            error_code=error.code,
            message=str(error),
        ).model_dump(mode="json"),
    )


def create_context_evidence_router() -> APIRouter:
    router = APIRouter(
        prefix="/api/v1/investigations/{run_id}/calls/{call_id}",
        tags=["context-evidence"],
    )
    responses: dict[int | str, dict[str, Any]] = {
        code: {"model": ApiErrorResponse} for code in (404, 409, 422)
    }

    @router.get("/evidence-catalog", response_model=EvidenceCatalog | None, responses=responses)
    async def catalog(
        run_id: UUID, call_id: UUID, request: Request
    ) -> EvidenceCatalog | None | JSONResponse:
        try:
            return await _reader(request, run_id, call_id).catalog()
        except TaskFactConflict as error:
            return _error(error)

    @router.get(
        "/evidence/{task_id}/{evidence_id}",
        response_model=ContextEvidenceDetail,
        responses=responses,
    )
    async def detail(
        run_id: UUID,
        call_id: UUID,
        task_id: UUID,
        evidence_id: Annotated[
            str, Path(min_length=1, max_length=100, pattern=r"^[a-z0-9][a-z0-9-]*$")
        ],
        snapshot_sha256: Annotated[str, Query(pattern=r"^[0-9a-f]{64}$")],
        request: Request,
    ) -> ContextEvidenceDetail | JSONResponse:
        try:
            return await _reader(request, run_id, call_id).read(
                EvidenceReadRequest(
                    task_id=task_id,
                    evidence_id=evidence_id,
                    snapshot_sha256=snapshot_sha256,
                )
            )
        except TaskFactConflict as error:
            return _error(error)

    return router
