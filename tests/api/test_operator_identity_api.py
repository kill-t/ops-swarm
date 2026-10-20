"""真实 PostgreSQL 验证控制面身份、权限、准入审计及原审批竞争语义。"""

import asyncio
import hashlib
import json
import re
from uuid import UUID, uuid4

import httpx2
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError

from opsswarm.approval.persistence.tables import ApprovalRequestRow
from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.identity.models import WRITE_PERMISSIONS
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.identity.store import OperatorAuthorizationStore
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentEventRow, IncidentRow
from opsswarm.investigation.scenarios import RELEASE_500_SCENARIO
from tests.api.test_control_plane_api import (
    ApiOperationAdapter,
    ApiVerificationAdapter,
    alert_payload,
    complete_scenario_investigation,
    fixed_clock,
)
from tests.identity_helpers import auth_header, auth_settings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def identity_client(database_session_factory, migrated_database_url):
    app = create_app(
        database_settings=DatabaseSettings(database_url=migrated_database_url),
        operator_auth_settings=auth_settings(),
        clock=fixed_clock,
        operation_adapter=ApiOperationAdapter(),
        verification_adapter=ApiVerificationAdapter(),
    )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
        ) as client,
    ):
        yield app, client


async def count_rows(factory, row):
    async with factory() as session:
        return await session.scalar(select(func.count()).select_from(row))


async def new_incident(client):
    response = await client.post(
        "/api/v1/alerts", json=alert_payload("identity-alert"), headers=auth_header("alert_source")
    )
    assert response.status_code == 201, response.text
    return response.json()["incident"]


async def pending_approval(client, factory):
    incident = await new_incident(client)
    started = await client.post(
        f"/api/v1/incidents/{incident['id']}/actions/start-investigation",
        json={"expected_version": 1, "reason": "可信工程师开始调查"},
        headers=auth_header("engineer"),
    )
    assert started.status_code == 200, started.text
    run_id = started.json()["investigation"]["id"]
    await complete_scenario_investigation(
        factory, run_id=run_id, scenario=RELEASE_500_SCENARIO, worker_id="identity-worker"
    )
    plan = await client.post(
        f"/api/v1/investigations/{run_id}/action-plans", headers=auth_header("engineer")
    )
    assert plan.status_code == 201, plan.text
    incident = await client.get(
        f"/api/v1/incidents/{incident['id']}", headers=auth_header("engineer")
    )
    submitted = await client.post(
        f"/api/v1/action-plans/{plan.json()['action_plan']['id']}/approval-requests",
        json={"expected_incident_version": incident.json()["version"], "reason": "提交精确方案"},
        headers=auth_header("engineer"),
    )
    assert submitted.status_code == 201, submitted.text
    return submitted.json()["approval_request"]


async def test_every_registered_api_rejects_anonymous_before_business_access(identity_client):
    app, client = identity_client
    checked = 0
    for template, operations in app.openapi()["paths"].items():
        if not template.startswith("/api/"):
            continue
        path = re.sub(r"\{[^}]+\}", str(uuid4()), template)
        for method in operations:
            response = await client.request(method, path)
            assert response.status_code == 401, (method, path, response.text)
            assert response.headers["www-authenticate"] == "Bearer"
            checked += 1
    assert checked >= 40
    assert (await client.get("/health")).status_code == 200


async def test_me_returns_only_trusted_subject_and_permissions(identity_client):
    _, client = identity_client
    response = await client.get("/api/v1/operators/me", headers=auth_header("approver"))
    assert response.status_code == 200
    assert response.json()["subject_id"] == "trusted-approver"
    assert response.json()["permissions"] == ["decide_approval", "read"]
    assert response.headers["cache-control"] == "no-store"
    assert "sha256" not in response.text


@pytest.mark.parametrize(
    "authorization", ["Basic abc", "Bearer unknown." + "x" * 40, "Bearer viewer.bad"]
)
async def test_invalid_tokens_fail_without_business_or_authorization_writes(
    identity_client,
    database_session_factory,
    authorization,
):
    _, client = identity_client
    response = await client.post(
        "/api/v1/alerts",
        json=alert_payload("invalid-identity"),
        headers={"Authorization": authorization},
    )
    assert response.status_code == 401
    assert await count_rows(database_session_factory, IncidentRow) == 0
    assert await count_rows(database_session_factory, OperatorAuthorizationRow) == 0


async def test_all_registered_writes_are_mapped_and_viewer_cannot_mutate(identity_client):
    app, client = identity_client
    writes = {
        template: operations["post"]["operationId"].split("_api", 1)[0]
        for template, operations in app.openapi()["paths"].items()
        if "post" in operations
    }
    assert set(writes.values()) == set(WRITE_PERMISSIONS)
    for template in writes:
        path = re.sub(r"\{[^}]+\}", str(uuid4()), template)
        response = await client.post(path, json={}, headers=auth_header("viewer"))
        assert response.status_code == 403, (path, response.text)


async def test_actor_is_injected_and_command_authorization_is_persisted(
    identity_client,
    database_session_factory,
):
    _, client = identity_client
    incident = await new_incident(client)
    path = f"/api/v1/incidents/{incident['id']}/actions/start-investigation"
    body = {"expected_version": 1, "reason": "服务端注入操作者"}
    response = await client.post(path, json=body, headers=auth_header("engineer"))
    assert response.status_code == 200, response.text
    async with database_session_factory() as session:
        grant = await session.get(
            OperatorAuthorizationRow, UUID(response.headers["x-authorization-id"])
        )
        event = await session.scalar(
            select(IncidentEventRow)
            .where(IncidentEventRow.incident_id == UUID(incident["id"]))
            .order_by(IncidentEventRow.id.desc())
            .limit(1)
        )
        assert event.actor_id == grant.subject_id == "trusted-engineer"
        assert grant.credential_id == "engineer"
        assert grant.role == "engineer" and grant.policy_version == "operator-auth-v1"
        assert "execute_action" not in grant.permissions
        assert grant.resources == {"incident_id": incident["id"]}
        assert (
            grant.body_sha256
            == hashlib.sha256(
                json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest()
        )


async def test_forged_actor_and_query_token_cannot_override_identity(
    identity_client,
    database_session_factory,
):
    _, client = identity_client
    incident = await new_incident(client)
    before = await count_rows(database_session_factory, OperatorAuthorizationRow)
    response = await client.post(
        f"/api/v1/incidents/{incident['id']}/actions/start-investigation",
        json={"expected_version": 1, "reason": "冒名", "actor_id": "another-engineer"},
        headers=auth_header("engineer"),
    )
    assert response.status_code == 403
    assert response.json()["error_code"] == "operator_subject_mismatch"
    assert await count_rows(database_session_factory, OperatorAuthorizationRow) == before
    assert (await client.get("/api/v1/operators/me?token=anything")).status_code == 401


async def test_authenticated_approval_preserves_idempotency_and_subject_audit(
    identity_client,
    database_session_factory,
):
    _, client = identity_client
    approval = await pending_approval(client, database_session_factory)
    incident = await client.get(
        f"/api/v1/incidents/{approval['incident_id']}", headers=auth_header("engineer")
    )
    body = {"expected_incident_version": incident.json()["version"], "reason": "可信人工确认"}
    url = f"/api/v1/approval-requests/{approval['id']}/actions/approve"
    first, second = await asyncio.gather(
        *[client.post(url, json=body, headers=auth_header("approver")) for _ in range(2)]
    )
    assert first.status_code == second.status_code == 200, (first.text, second.text)
    assert {first.json()["decision_applied"], second.json()["decision_applied"]} == {True, False}
    assert first.headers["x-authorization-id"] != second.headers["x-authorization-id"]
    assert (await client.post(url, json=body, headers=auth_header("engineer"))).status_code == 403
    async with database_session_factory() as session:
        row = await session.get(ApprovalRequestRow, UUID(approval["id"]))
        assert row.requested_by_id == "trusted-engineer"
        assert row.decided_by_id == "trusted-approver"
    rejected = await client.post(
        url.replace("approve", "reject"), json=body, headers=auth_header("approver")
    )
    assert rejected.status_code == 409
    assert "x-authorization-id" in rejected.headers


async def test_stale_incident_version_is_still_rejected(identity_client):
    _, client = identity_client
    incident = await new_incident(client)
    response = await client.post(
        f"/api/v1/incidents/{incident['id']}/actions/start-investigation",
        json={"expected_version": 2, "reason": "版本冲突"},
        headers=auth_header("engineer"),
    )
    assert response.status_code == 409
    assert response.json()["error_code"] == "incident_version_conflict"
    assert "x-authorization-id" in response.headers


async def test_audit_write_failure_prevents_business_mutation(
    identity_client,
    monkeypatch,
    database_session_factory,
):
    _, client = identity_client

    async def broken(*_args, **_kwargs):
        raise OperationalError("audit unavailable", {}, RuntimeError("offline"))

    monkeypatch.setattr(OperatorAuthorizationStore, "record", broken)
    response = await client.post(
        "/api/v1/alerts", json=alert_payload("audit-outage"), headers=auth_header("alert_source")
    )
    assert response.status_code == 503
    assert response.json()["error_code"] == "operator_audit_unavailable"
    assert "offline" not in response.text
    assert await count_rows(database_session_factory, IncidentRow) == 0


async def test_legacy_requires_actor_and_cannot_claim_trusted_identity(migrated_database_url):
    app = create_app(
        database_settings=DatabaseSettings(database_url=migrated_database_url),
        operator_auth_settings=OperatorAuthSettings(mode="legacy_local", credentials=()),
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        assert (await client.get("/api/v1/operators/me")).status_code == 401
        assert (
            await client.get("/api/v1/incidents", headers=auth_header("viewer"))
        ).status_code == 401
        response = await client.post(
            f"/api/v1/incidents/{uuid4()}/actions/start-investigation",
            json={"expected_version": 1, "reason": "遗漏本地审计主体"},
        )
        assert response.status_code == 422


async def test_expired_and_duplicate_headers_are_rejected(identity_client):
    app, client = identity_client
    app.state.operator_clock = lambda: auth_settings().credentials[0].expires_at
    assert (
        await client.get("/api/v1/operators/me", headers=auth_header("viewer"))
    ).status_code == 401
    app.state.operator_clock = fixed_clock
    response = await client.get(
        "/api/v1/operators/me",
        headers=[("Authorization", auth_header("viewer")["Authorization"])] * 2,
    )
    assert response.status_code == 401


async def test_unknown_write_route_is_denied_by_default(identity_client):
    app, client = identity_client

    @app.post("/api/v1/unregistered-command")
    async def unregistered_command():
        raise AssertionError("未登记命令不能执行")

    response = await client.post("/api/v1/unregistered-command", headers=auth_header("engineer"))
    assert response.status_code == 403


@pytest.mark.parametrize("mounted", [False, True])
async def test_deployment_prefix_cannot_bypass_authentication_or_permissions(
    identity_client,
    mounted,
    database_session_factory,
):
    app, _ = identity_client
    prefix = "/nested" if mounted else "/prefix"
    if mounted:
        root = FastAPI()
        root.mount(prefix, app)
        transport = httpx2.ASGITransport(app=root)
    else:
        transport = httpx2.ASGITransport(app=app, root_path=prefix)
    async with httpx2.AsyncClient(transport=transport, base_url="http://testserver") as client:
        assert (await client.get(f"{prefix}/api/v1/incidents")).status_code == 401
        response = await client.post(
            f"{prefix}/api/v1/alerts",
            json=alert_payload("prefixed-alert"),
            headers=auth_header("viewer"),
        )
        assert response.status_code == 403
    assert await count_rows(database_session_factory, IncidentRow) == 0
