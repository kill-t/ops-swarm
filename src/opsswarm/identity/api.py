"""控制面统一认证依赖、人工主体注入和身份自查接口。"""

import json
from typing import Annotated, cast
from uuid import UUID

from fastapi import APIRouter, Depends, Request, Response
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import AwareDatetime, BaseModel, ConfigDict
from sqlalchemy.exc import SQLAlchemyError

from opsswarm.identity.authentication import authenticate_operator
from opsswarm.identity.models import (
    HUMAN_OPERATIONS,
    TRUSTED_OPERATIONS,
    OperatorAccessError,
    OperatorPermission,
    OperatorPrincipal,
    OperatorRole,
    require_permission,
)
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.identity.store import OperatorAuthorizationStore

bearer_scheme = HTTPBearer(auto_error=False, scheme_name="OperatorBearer")


class OperatorIdentityResponse(BaseModel):
    """身份自查只返回认证结果和授权范围，不序列化凭据配置。"""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_version: str = "operator-auth-v1"
    subject_id: str
    credential_id: str
    role: OperatorRole
    permissions: tuple[OperatorPermission, ...]
    expires_at: AwareDatetime


async def authorize_control_request(
    request: Request,
    _credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> None:
    """统一处理控制面业务路由；命令准入必须早于业务仓储及幂等返回。"""

    route = cast(APIRoute, request.scope["route"])
    # URL 包含 ASGI 部署前缀，权限边界必须依据实际匹配的业务路由。
    if not route.path.startswith("/api/"):
        return
    settings = cast(OperatorAuthSettings, request.app.state.operator_auth_settings)
    headers = request.headers.getlist("authorization")
    trusted_call = False
    if route.name in {"read_model_input", "read_context_manifest", "read_interaction"}:
        from opsswarm.investigation.budget.queries import CallBudgetQueries

        queries = getattr(request.app.state, "call_budget_queries", None)
        if isinstance(queries, CallBudgetQueries):
            try:
                run_id = UUID(str(request.path_params["run_id"]))
                call_id = UUID(str(request.path_params["call_id"]))
            except (ValueError, KeyError):
                pass
            else:
                trusted_call = await queries.requires_trusted_read(run_id, call_id)
    if (
        settings.mode == "legacy_local"
        and route.name not in TRUSTED_OPERATIONS
        and not trusted_call
    ):
        if headers:
            raise OperatorAccessError("operator_unauthenticated", "当前实例未启用可信认证")
        return
    if len(headers) != 1:
        raise OperatorAccessError("operator_unauthenticated", "需要唯一操作者认证")
    principal = authenticate_operator(settings, headers[0], request.app.state.operator_clock())
    request.state.operator_principal = principal
    if route.name == "get_operator_identity":
        return
    require_permission(principal, request.method, route.name)
    if request.method == "GET":
        return
    body = await request.body()
    if len(body) > 1024 * 1024:
        raise OperatorAccessError("operator_command_too_large", "控制面命令超过大小上限", 413)
    if route.name in HUMAN_OPERATIONS:
        _assert_subject(body, principal)
    store = getattr(request.app.state, "operator_authorization_store", None)
    if not isinstance(store, OperatorAuthorizationStore):
        raise OperatorAccessError("operator_audit_unavailable", "授权审计尚未初始化", 503)
    try:
        request.state.authorization_id = await store.record(
            principal,
            operation=route.name,
            method=request.method,
            route=route.path,
            resources={name: str(value) for name, value in request.path_params.items()},
            body=body,
            authorized_at=request.app.state.operator_clock(),
        )
    except SQLAlchemyError:
        raise OperatorAccessError("operator_audit_unavailable", "授权审计无法持久化", 503) from None


def _assert_subject(body: bytes, principal: OperatorPrincipal) -> None:
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        raise OperatorAccessError(
            "operator_command_invalid", "命令正文必须为 JSON 对象", 422
        ) from None
    if not isinstance(payload, dict):
        raise OperatorAccessError("operator_command_invalid", "命令正文必须为 JSON 对象", 422)
    asserted = payload.get("actor_id")
    if asserted is not None and (
        not isinstance(asserted, str) or asserted.strip() != principal.subject_id
    ):
        raise OperatorAccessError("operator_subject_mismatch", "操作者声明与认证主体不一致", 403)


async def resolve_human_actor(request: Request) -> str:
    """认证模式只使用系统主体；兼容模式仍要求显式本地审计标识。"""

    principal = getattr(request.state, "operator_principal", None)
    if isinstance(principal, OperatorPrincipal):
        return principal.subject_id
    payload = await request.json()
    actor_id = payload.get("actor_id") if isinstance(payload, dict) else None
    if not isinstance(actor_id, str) or not 1 <= len(actor_id.strip()) <= 100:
        raise OperatorAccessError("operator_actor_required", "兼容模式必须提供 actor_id", 422)
    return actor_id.strip()


HumanActorDependency = Annotated[str, Depends(resolve_human_actor)]


def create_identity_router() -> APIRouter:
    router = APIRouter()

    @router.get("/api/v1/operators/me", tags=["操作者身份"])
    async def get_operator_identity(
        request: Request, response: Response
    ) -> OperatorIdentityResponse:
        principal = cast(OperatorPrincipal, request.state.operator_principal)
        response.headers["Cache-Control"] = "no-store"
        return OperatorIdentityResponse(
            subject_id=principal.subject_id,
            credential_id=principal.credential_id,
            role=principal.role,
            permissions=principal.permissions,
            expires_at=principal.expires_at,
        )

    return router
