"""共享调用预算及预留记录的只读路由。"""

from uuid import UUID

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from opsswarm.control_plane.schemas import ApiErrorResponse
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, require_permission
from opsswarm.investigation.budget.contracts import CallBudgetSnapshot
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.candidate.contracts import CandidateManifest
from opsswarm.investigation.context.contracts import ContextManifest
from opsswarm.investigation.human_prepare.contracts import PreparationManifest
from opsswarm.investigation.loop.contracts import AnyModelInput, CallInteraction
from opsswarm.investigation.reinvestigation_candidate.contracts import ReflowCandidateManifest
from opsswarm.investigation.reinvestigation_coordination.contracts import ReflowManifest
from opsswarm.investigation.reinvestigation_execution.contracts import ExecutionManifest
from opsswarm.investigation.reinvestigation_review.contracts import ReflowReviewManifest
from opsswarm.investigation.reinvestigation_synthesis.contracts import SynthesisManifest
from opsswarm.investigation.tasks.errors import TaskFactConflict


async def _require_trusted_material_read(
    request: Request, queries: CallBudgetQueries, run_id: UUID, call_id: UUID
) -> None:
    # 保留旧演练路径的公开读取，人工重评、候选及回流材料始终要求可信主体。
    if await queries.requires_trusted_read(run_id, call_id):
        principal = getattr(request.state, "operator_principal", None)
        if not isinstance(principal, OperatorPrincipal):
            raise OperatorAccessError("operator_unauthenticated", "读取可信阶段材料需要可信身份")
        require_permission(principal, "GET", "get_reinvestigation_coordination")


def create_call_budget_router() -> APIRouter:
    router = APIRouter(tags=["investigation-budget"])

    @router.get(
        "/api/v1/investigations/{run_id}/call-budget",
        response_model=CallBudgetSnapshot,
        responses={404: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    async def read_budget(run_id: UUID, request: Request) -> CallBudgetSnapshot:
        queries = getattr(request.app.state, "call_budget_queries", None)
        if not isinstance(queries, CallBudgetQueries):
            raise RuntimeError("CallBudgetQueries 尚未初始化")
        return await queries.get_snapshot(run_id)

    @router.get(
        "/api/v1/investigations/{run_id}/calls/{call_id}/model-input",
        response_model=AnyModelInput | None,
        responses={404: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    async def read_model_input(
        run_id: UUID, call_id: UUID, request: Request
    ) -> AnyModelInput | None | JSONResponse:
        queries = getattr(request.app.state, "call_budget_queries", None)
        if not isinstance(queries, CallBudgetQueries):
            raise RuntimeError("CallBudgetQueries 尚未初始化")
        await _require_trusted_material_read(request, queries, run_id, call_id)
        try:
            return await queries.get_model_input(run_id, call_id)
        except TaskFactConflict as error:
            if error.code != "model_input_not_found":
                raise
            return JSONResponse(
                status_code=404,
                content=ApiErrorResponse(error_code=error.code, message=str(error)).model_dump(
                    mode="json"
                ),
            )

    @router.get(
        "/api/v1/investigations/{run_id}/calls/{call_id}/context-manifest",
        response_model=ContextManifest
        | PreparationManifest
        | CandidateManifest
        | ReflowManifest
        | ExecutionManifest
        | SynthesisManifest
        | ReflowCandidateManifest
        | ReflowReviewManifest
        | None,
        responses={404: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    async def read_context_manifest(
        run_id: UUID,
        call_id: UUID,
        request: Request,
    ) -> (
        ContextManifest
        | PreparationManifest
        | CandidateManifest
        | ReflowManifest
        | ExecutionManifest
        | SynthesisManifest
        | ReflowCandidateManifest
        | ReflowReviewManifest
        | None
        | JSONResponse
    ):
        queries = getattr(request.app.state, "call_budget_queries", None)
        if not isinstance(queries, CallBudgetQueries):
            raise RuntimeError("CallBudgetQueries 尚未初始化")
        await _require_trusted_material_read(request, queries, run_id, call_id)
        try:
            return await queries.get_context_manifest(run_id, call_id)
        except TaskFactConflict as error:
            if error.code != "context_manifest_not_found":
                raise
            return JSONResponse(
                status_code=404,
                content=ApiErrorResponse(
                    error_code=error.code,
                    message=str(error),
                ).model_dump(mode="json"),
            )

    @router.get(
        "/api/v1/investigations/{run_id}/calls/{call_id}/interaction",
        response_model=CallInteraction,
        responses={404: {"model": ApiErrorResponse}, 422: {"model": ApiErrorResponse}},
    )
    async def read_interaction(
        run_id: UUID, call_id: UUID, request: Request
    ) -> CallInteraction | JSONResponse:
        queries = getattr(request.app.state, "call_budget_queries", None)
        if not isinstance(queries, CallBudgetQueries):
            raise RuntimeError("CallBudgetQueries 尚未初始化")
        await _require_trusted_material_read(request, queries, run_id, call_id)
        try:
            return await queries.get_interaction(run_id, call_id)
        except TaskFactConflict as error:
            if error.code != "call_interaction_not_found":
                raise
            return JSONResponse(
                status_code=404,
                content=ApiErrorResponse(error_code=error.code, message=str(error)).model_dump(
                    mode="json"
                ),
            )

    return router
