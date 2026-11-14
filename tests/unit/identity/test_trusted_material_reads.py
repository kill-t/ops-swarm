"""通用材料入口不能绕过可信阶段，旧调查公开读取仍兼容。"""

from datetime import UTC, datetime, timedelta
from hashlib import sha256
from uuid import uuid4

import httpx2
import pytest
from fastapi import Depends, FastAPI

from opsswarm.control_plane.error_handlers import register_exception_handlers
from opsswarm.identity.api import authorize_control_request
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.investigation.budget.api import create_call_budget_router
from opsswarm.investigation.budget.queries import CallBudgetQueries


class Session:
    def __init__(self, operation):
        self.operation = operation

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def scalar(self, statement):
        return self.operation


class Queries(CallBudgetQueries):
    def __init__(self, operation):
        super().__init__(lambda: Session(operation))
        self.read = False

    async def get_model_input(self, *args):
        self.read = True
        return None

    async def get_context_manifest(self, *args):
        self.read = True
        return None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["legacy_local", "bearer"])
@pytest.mark.parametrize(
    "operation", ["human_review", "evaluate_candidate", "coordinate_reinvestigation"]
)
@pytest.mark.parametrize("suffix", ["model-input", "context-manifest", "interaction"])
async def test_sensitive_material_anonymous_read_is_rejected(mode, operation, suffix):
    app = FastAPI(dependencies=[Depends(authorize_control_request)])
    register_exception_handlers(app)
    app.include_router(create_call_budget_router())
    # 身份配置不需要凭据即可验证匿名准入拒绝。
    app.state.operator_auth_settings = OperatorAuthSettings.model_construct(
        mode=mode, credentials=()
    )
    queries = Queries(operation)
    app.state.call_budget_queries = queries
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/v1/investigations/{uuid4()}/calls/{uuid4()}/{suffix}")
    assert response.status_code == 401
    assert not queries.read


@pytest.mark.asyncio
async def test_legacy_investigation_material_remains_public():
    app = FastAPI(dependencies=[Depends(authorize_control_request)])
    register_exception_handlers(app)
    app.include_router(create_call_budget_router())
    app.state.operator_auth_settings = OperatorAuthSettings(mode="legacy_local", credentials=[])
    queries = Queries("analyze_log")
    app.state.call_budget_queries = queries
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(f"/api/v1/investigations/{uuid4()}/calls/{uuid4()}/model-input")
    assert response.status_code == 200
    assert queries.read


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "role", "status"),
    [
        ("bearer", "viewer", 200),
        ("bearer", "alert_source", 403),
        ("legacy_local", "viewer", 401),
    ],
)
async def test_sensitive_material_requires_read_permission(mode, role, status):
    app = FastAPI(dependencies=[Depends(authorize_control_request)])
    register_exception_handlers(app)
    app.include_router(create_call_budget_router())
    now = datetime.now(UTC)
    secret = "a" * 34
    app.state.operator_auth_settings = OperatorAuthSettings(
        mode=mode,
        credentials=[
            dict(
                credential_id="fixture",
                subject_id="fixture",
                role=role,
                secret_sha256=sha256(secret.encode()).hexdigest(),
                expires_at=now + timedelta(hours=1),
            )
        ],
    )
    app.state.operator_clock = lambda: now
    queries = Queries("human_review")
    app.state.call_budget_queries = queries
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            f"/api/v1/investigations/{uuid4()}/calls/{uuid4()}/model-input",
            headers={"Authorization": f"Bearer fixture.{secret}"},
        )
    assert response.status_code == status
    assert queries.read is (status == 200)
