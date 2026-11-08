"""通过真实 ASGI 边界核对错误契约，不启动数据库或业务长链。"""

from datetime import UTC, datetime
from uuid import UUID

import httpx2
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict

from opsswarm.approval import ApprovalRequestExpired
from opsswarm.approval.reinvestigation_decision.contracts import ReflowDecisionError
from opsswarm.control_plane import app as app_module
from opsswarm.control_plane.error_handlers import register_exception_handlers
from opsswarm.execution import OperationExecutionFailed
from opsswarm.execution.candidate.contracts import PreflightError
from opsswarm.experience.contracts import ExperienceError
from opsswarm.identity.models import OperatorAccessError
from opsswarm.identity.settings import OperatorAuthSettings, OperatorCredential
from opsswarm.incidents import IncidentDomainError, IncidentNotFound, IncidentVersionConflict
from opsswarm.investigation.human_wait.contracts import HumanWaitError
from opsswarm.observability import TracingRuntime
from opsswarm.reports import IncidentReportIntegrityViolation

pytestmark = pytest.mark.asyncio
IDENTITY = UUID(int=1)


async def error_response(error: Exception) -> httpx2.Response:
    app = FastAPI()
    register_exception_handlers(app)

    @app.get("/failure")
    async def failure() -> None:
        raise error

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        return await client.get("/failure")


class ChildIncidentNotFound(IncidentNotFound):
    """错误注册必须继续按异常继承关系匹配。"""


@pytest.mark.parametrize(
    ("error", "status", "code", "details"),
    [
        (ChildIncidentNotFound("missing"), 404, "incident_not_found", {"incident_id": "missing"}),
        (
            IncidentVersionConflict(expected_version=2, actual_version=3),
            409,
            "incident_version_conflict",
            {"expected_version": 2, "actual_version": 3},
        ),
        (
            ApprovalRequestExpired(IDENTITY, expires_at="2026-11-08T00:00:00+00:00"),
            409,
            "approval_request_expired",
            {"request_id": str(IDENTITY), "expires_at": "2026-11-08T00:00:00+00:00"},
        ),
        (
            OperationExecutionFailed(IDENTITY, error_code="adapter_timeout", message="远端失败"),
            502,
            "operation_execution_failed",
            {"execution_id": str(IDENTITY), "operation_error_code": "adapter_timeout"},
        ),
        (
            IncidentReportIntegrityViolation(IDENTITY),
            500,
            "incident_report_integrity_violation",
            {"report_id": str(IDENTITY)},
        ),
        (IncidentDomainError("公开错误"), 400, "incident_domain_error", {}),
    ],
)
async def test_historical_errors_keep_status_details_and_message(error, status, code, details):
    response = await error_response(error)
    assert response.status_code == status
    assert response.json() == dict(error_code=code, message=str(error), details=details)


@pytest.mark.parametrize("error_type", [HumanWaitError, PreflightError, ReflowDecisionError])
@pytest.mark.parametrize("status", [404, 409, 503])
async def test_coded_errors_keep_explicit_status_and_public_fields(error_type, status):
    response = await error_response(error_type("public_code", "公开原因", status))
    assert response.status_code == status
    assert response.json() == dict(error_code="public_code", message="公开原因", details={})


@pytest.mark.parametrize("status", [401, 403, 503])
async def test_only_unauthenticated_response_includes_bearer_challenge(status):
    response = await error_response(OperatorAccessError("operator_denied", "公开原因", status))
    assert response.status_code == status
    assert response.headers.get("WWW-Authenticate") == ("Bearer" if status == 401 else None)
    assert response.json()["details"] == {}


async def test_experience_error_uses_its_public_message():
    error = ExperienceError("experience_unavailable", "公开原因", 409)
    error.args = ("异常内部字符串",)
    response = await error_response(error)
    assert response.json()["message"] == "公开原因"


class ValidationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    count: int


async def test_fastapi_validation_omits_submitted_values():
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/input")
    async def read_input(payload: ValidationInput) -> dict:
        return payload.model_dump()

    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post("/input", json=dict(count="private-value", extra="private"))
    assert response.status_code == 422
    assert response.json()["error_code"] == "request_validation_error"
    issues = response.json()["details"]["issues"]
    assert {item["location"] for item in issues} == {"body.count", "body.extra"}
    assert all(set(item) == {"location", "message", "type"} for item in issues)
    assert "private" not in response.text


async def test_validation_context_and_original_body_are_not_serialized():
    error = RequestValidationError(
        [
            dict(
                loc=("body", "items", 0),
                msg="公开失败",
                type="value_error",
                input="private-input",
                ctx={"error": ValueError("private-context")},
            )
        ],
        body=dict(secret="private-body"),
    )
    response = await error_response(error)
    assert response.status_code == 422
    assert response.json()["details"] == dict(
        issues=[dict(location="body.items.0", message="公开失败", type="value_error")]
    )
    assert "private" not in response.text


async def test_unknown_exception_and_framework_http_error_keep_their_own_handling():
    with pytest.raises(RuntimeError, match="内部错误"):
        await error_response(RuntimeError("内部错误"))
    response = await error_response(HTTPException(418, "框架错误", headers={"X-Probe": "original"}))
    assert response.status_code == 418
    assert response.json() == dict(detail="框架错误")
    assert response.headers["X-Probe"] == "original"


async def test_app_factory_installs_handlers_and_preserves_response_metadata(monkeypatch):
    settings = OperatorAuthSettings(
        mode="bearer",
        credentials=(
            OperatorCredential(
                credential_id="test-engineer",
                subject_id="test-engineer",
                role="engineer",
                secret_sha256="0" * 64,
                expires_at=datetime(2030, 1, 1, tzinfo=UTC),
            ),
        ),
    )
    app = app_module.create_app(operator_auth_settings=settings, tracing_runtime=TracingRuntime())
    monkeypatch.setattr(app_module, "current_trace_id_hex", lambda: "a" * 32)

    @app.get("/error-probe")
    async def failure(request: Request) -> None:
        request.state.authorization_id = IDENTITY
        raise PreflightError("preflight_blocked", "尚未核验", 409)

    # 不进入 lifespan：这里只验证协议装配，不声称验证数据库生命周期。
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get("/error-probe")
        assert response.status_code == 409
        assert response.json()["error_code"] == "preflight_blocked"
        assert response.headers["X-Authorization-ID"] == str(IDENTITY)
        assert response.headers["X-Trace-ID"] == "a" * 32
        missing_auth = await client.get("/api/v1/operators/me")
        assert missing_auth.status_code == 401
        assert missing_auth.headers["WWW-Authenticate"] == "Bearer"
        assert (await client.get("/health")).status_code == 200
