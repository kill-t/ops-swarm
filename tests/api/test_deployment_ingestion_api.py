"""受认证 CI/CD 部署事件 API 的真实 PostgreSQL 集成测试。"""

import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.change_records.app import create_app
from opsswarm.change_records.deployment_ingestion import DeploymentIngestionSettings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)
SECRET = "integration-test-secret-value-1234567890"
KEY_ID = "github-actions-production"
REPOSITORY = "kill-t/ops-swarm"
COMMIT_SHA = "a" * 40


def _settings(*, enabled: bool = True) -> DeploymentIngestionSettings:
    return DeploymentIngestionSettings(
        enabled=enabled,
        key_id=KEY_ID,
        secret=SECRET,
        repository=REPOSITORY,
    )


def _payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "delivery_id": "delivery-1",
        "environment": "fault-lab",
        "service_name": "order-service",
        "repository": REPOSITORY,
        "commit_sha": COMMIT_SHA,
        "from_version": "stable",
        "to_version": "rc1",
        "deployment_status": "succeeded",
        "deployed_at": (NOW - timedelta(minutes=1)).isoformat(),
    }
    payload.update(overrides)
    return payload


def _signed_request(
    payload: dict[str, Any],
    *,
    timestamp: int | None = None,
    secret: str = SECRET,
    key_id: str = KEY_ID,
) -> tuple[bytes, dict[str, str]]:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    signed_timestamp = timestamp if timestamp is not None else int(NOW.timestamp())
    signature = hmac.new(
        secret.encode(),
        str(signed_timestamp).encode() + b"." + body,
        hashlib.sha256,
    ).hexdigest()
    return body, {
        "Content-Type": "application/json",
        "X-OpsSwarm-Key-Id": key_id,
        "X-OpsSwarm-Timestamp": str(signed_timestamp),
        "X-OpsSwarm-Signature": f"sha256={signature}",
    }


async def test_signed_deployment_is_persisted_and_exact_replay_is_idempotent(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(
        database_session_factory,
        clock=lambda: NOW,
        ingestion_settings=_settings(),
    )
    documented_responses = app.openapi()["paths"]["/api/v1/deployment-events"]["post"]["responses"]
    assert {"200", "201", "400", "401", "403", "409", "413", "415", "422", "503"} <= set(
        documented_responses
    )
    body, headers = _signed_request(_payload())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        created = await client.post("/api/v1/deployment-events", content=body, headers=headers)
        duplicate = await client.post("/api/v1/deployment-events", content=body, headers=headers)
        listed = await client.get(
            "/api/v1/changes",
            params={"service_name": "order-service", "lookback_hours": 6},
        )

    assert created.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["record"] == created.json()["record"]
    record = listed.json()["items"][0]
    assert record["source"] == "ci-deployment-ingestion"
    assert record["source_identity"] == KEY_ID
    assert record["occurred_at"] == "2026-08-30T11:59:00Z"
    assert record["recorded_at"] == "2026-08-30T12:00:00Z"
    assert record["change"]["code_reference"] == {
        "repository": REPOSITORY,
        "commit_sha": COMMIT_SHA,
    }


async def test_delivery_id_reuse_with_content_or_deployment_time_drift_returns_409(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(
        database_session_factory,
        clock=lambda: NOW,
        ingestion_settings=_settings(),
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        body, headers = _signed_request(_payload())
        assert (
            await client.post("/api/v1/deployment-events", content=body, headers=headers)
        ).status_code == 201

        changed_body, changed_headers = _signed_request(_payload(to_version="rc2"))
        content_conflict = await client.post(
            "/api/v1/deployment-events", content=changed_body, headers=changed_headers
        )
        time_body, time_headers = _signed_request(
            _payload(deployed_at=(NOW - timedelta(minutes=2)).isoformat())
        )
        time_conflict = await client.post(
            "/api/v1/deployment-events", content=time_body, headers=time_headers
        )

    assert content_conflict.status_code == 409
    assert time_conflict.status_code == 409


@pytest.mark.parametrize(
    ("header_mutation", "expected_code"),
    [
        (lambda headers: headers.pop("X-OpsSwarm-Signature"), "deployment_authentication_failed"),
        (
            lambda headers: headers.update({"X-OpsSwarm-Key-Id": "untrusted-pipeline"}),
            "deployment_authentication_failed",
        ),
        (
            lambda headers: headers.update({"X-OpsSwarm-Signature": "sha256=" + "0" * 64}),
            "deployment_authentication_failed",
        ),
    ],
)
async def test_missing_or_invalid_authentication_fails_without_leaking_configuration(
    database_session_factory: async_sessionmaker[AsyncSession],
    header_mutation: Any,
    expected_code: str,
) -> None:
    app = create_app(
        database_session_factory,
        clock=lambda: NOW,
        ingestion_settings=_settings(),
    )
    body, headers = _signed_request(_payload())
    header_mutation(headers)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        response = await client.post("/api/v1/deployment-events", content=body, headers=headers)

    assert response.status_code == 401
    assert response.json()["error_code"] == expected_code
    assert response.headers["www-authenticate"] == "HMAC-SHA256"
    assert SECRET not in response.text
    assert REPOSITORY not in response.text


async def test_signature_age_repository_and_event_age_are_enforced(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(
        database_session_factory,
        clock=lambda: NOW,
        ingestion_settings=_settings(),
    )
    stale_body, stale_headers = _signed_request(
        _payload(), timestamp=int((NOW - timedelta(minutes=6)).timestamp())
    )
    forbidden_body, forbidden_headers = _signed_request(_payload(repository="kill-t/other"))
    old_body, old_headers = _signed_request(
        _payload(deployed_at=(NOW - timedelta(days=2)).isoformat())
    )
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        stale = await client.post(
            "/api/v1/deployment-events", content=stale_body, headers=stale_headers
        )
        forbidden = await client.post(
            "/api/v1/deployment-events", content=forbidden_body, headers=forbidden_headers
        )
        old = await client.post("/api/v1/deployment-events", content=old_body, headers=old_headers)

    assert stale.status_code == 401
    assert stale.json()["error_code"] == "deployment_signature_expired"
    assert forbidden.status_code == 403
    assert forbidden.json()["error_code"] == "deployment_repository_forbidden"
    assert old.status_code == 422
    assert old.json()["error_code"] == "deployment_event_timestamp_invalid"


async def test_endpoint_is_disabled_by_default_and_rejects_non_json_or_oversized_body(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    disabled_app = create_app(database_session_factory, clock=lambda: NOW)
    enabled_app = create_app(
        database_session_factory,
        clock=lambda: NOW,
        ingestion_settings=_settings(),
    )
    body, headers = _signed_request(_payload())
    oversized = b"{" + b" " * (16 * 1024) + b"}"
    oversized_signature = hmac.new(
        SECRET.encode(),
        str(int(NOW.timestamp())).encode() + b"." + oversized,
        hashlib.sha256,
    ).hexdigest()
    oversized_headers = {
        **headers,
        "X-OpsSwarm-Signature": f"sha256={oversized_signature}",
    }
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=disabled_app),
        base_url="http://testserver",
    ) as client:
        disabled = await client.post("/api/v1/deployment-events", content=body, headers=headers)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=enabled_app),
        base_url="http://testserver",
    ) as client:
        non_json = await client.post(
            "/api/v1/deployment-events",
            content=body,
            headers={**headers, "Content-Type": "text/plain"},
        )
        too_large = await client.post(
            "/api/v1/deployment-events",
            content=oversized,
            headers=oversized_headers,
        )

    assert disabled.status_code == 503
    assert disabled.json()["error_code"] == "deployment_ingestion_disabled"
    assert non_json.status_code == 415
    assert too_large.status_code == 413
