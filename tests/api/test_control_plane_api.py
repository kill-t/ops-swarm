"""控制面 API 与真实 PostgreSQL 的集成测试。"""

from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import httpx2
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

BASE_TIME = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)


def fixed_clock() -> datetime:
    return BASE_TIME


def alert_payload(
    external_event_id: str,
    *,
    fingerprint: str = "order-service:http-500",
) -> dict[str, object]:
    return {
        "source": "fault-simulator",
        "external_event_id": external_event_id,
        "fingerprint": fingerprint,
        "service_name": "order-service",
        "severity": "p1",
        "title": "订单接口大量返回 500",
        "payload": {"status_code": 500},
        "occurred_at": "2026-01-01T07:59:59+00:00",
    }


@pytest_asyncio.fixture
async def control_client(
    migrated_database_url: str,
    database_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx2.AsyncClient]:
    """通过正式应用生命周期创建和释放数据库连接池。"""

    _ = database_session_factory
    app = create_app(
        database_settings=DatabaseSettings(database_url=migrated_database_url),
        clock=fixed_clock,
    )
    async with app.router.lifespan_context(app):
        transport = httpx2.ASGITransport(app=app)
        async with httpx2.AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as client:
            yield client


async def test_health_endpoint(control_client: httpx2.AsyncClient) -> None:
    response = await control_client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "service": "opsswarm-control-plane",
        "status": "healthy",
    }


async def test_new_alert_returns_201_and_persisted_incident(
    control_client: httpx2.AsyncClient,
) -> None:
    response = await control_client.post("/api/v1/alerts", json=alert_payload("alert-001"))

    assert response.status_code == 201
    body = response.json()
    assert body["deduplicated"] is False
    assert body["created_incident"] is True
    assert body["incident"]["reference"] == "INC-000001"
    assert body["incident"]["status"] == "open"
    assert body["incident"]["version"] == 1
    assert body["incident"]["created_at"] == "2026-01-01T08:00:00Z"

    detail = await control_client.get(f"/api/v1/incidents/{body['incident']['id']}")
    assert detail.status_code == 200
    assert detail.json() == body["incident"]


async def test_duplicate_alert_returns_200_and_original_identifiers(
    control_client: httpx2.AsyncClient,
) -> None:
    payload = alert_payload("alert-001")

    first = await control_client.post("/api/v1/alerts", json=payload)
    duplicate = await control_client.post("/api/v1/alerts", json=payload)

    assert first.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["alert_id"] == first.json()["alert_id"]
    assert duplicate.json()["incident"]["id"] == first.json()["incident"]["id"]


async def test_same_fingerprint_new_event_returns_201_and_attaches_timeline(
    control_client: httpx2.AsyncClient,
) -> None:
    first = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-001"),
    )
    attached = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-002"),
    )

    assert attached.status_code == 201
    assert attached.json()["created_incident"] is False
    assert attached.json()["deduplicated"] is False
    assert attached.json()["incident"]["id"] == first.json()["incident"]["id"]

    events = await control_client.get(f"/api/v1/incidents/{first.json()['incident']['id']}/events")
    assert [item["event_type"] for item in events.json()["items"]] == [
        "incident.created",
        "incident.alert_attached",
    ]


async def test_incident_list_uses_stable_descending_cursor(
    control_client: httpx2.AsyncClient,
) -> None:
    for number in range(1, 4):
        response = await control_client.post(
            "/api/v1/alerts",
            json=alert_payload(
                f"alert-{number:03d}",
                fingerprint=f"order-service:http-500:{number}",
            ),
        )
        assert response.status_code == 201

    first_page = await control_client.get("/api/v1/incidents", params={"limit": 2})

    assert first_page.status_code == 200
    assert [item["display_number"] for item in first_page.json()["items"]] == [3, 2]
    assert first_page.json()["next_before_display_number"] == 2

    second_page = await control_client.get(
        "/api/v1/incidents",
        params={"limit": 2, "before_display_number": 2},
    )
    assert [item["display_number"] for item in second_page.json()["items"]] == [1]
    assert second_page.json()["next_before_display_number"] is None


async def test_start_investigation_and_status_filter(
    control_client: httpx2.AsyncClient,
) -> None:
    created = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-001"),
    )
    incident_id = created.json()["incident"]["id"]

    started = await control_client.post(
        f"/api/v1/incidents/{incident_id}/actions/start-investigation",
        json={
            "expected_version": 1,
            "actor_id": "engineer-001",
            "reason": "值班工程师开始调查",
        },
    )

    assert started.status_code == 200
    assert started.json()["status"] == "investigating"
    assert started.json()["version"] == 2

    filtered = await control_client.get(
        "/api/v1/incidents",
        params={"status": "investigating"},
    )
    assert [item["id"] for item in filtered.json()["items"]] == [incident_id]


async def test_stale_version_and_illegal_transition_return_distinct_409_errors(
    control_client: httpx2.AsyncClient,
) -> None:
    created = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-001"),
    )
    incident_id = created.json()["incident"]["id"]
    action_url = f"/api/v1/incidents/{incident_id}/actions/start-investigation"
    request_body = {
        "expected_version": 1,
        "actor_id": "engineer-001",
        "reason": "开始调查",
    }

    await control_client.post(action_url, json=request_body)
    stale = await control_client.post(action_url, json=request_body)
    illegal = await control_client.post(
        action_url,
        json={**request_body, "expected_version": 2},
    )

    assert stale.status_code == 409
    assert stale.json()["error_code"] == "incident_version_conflict"
    assert stale.json()["details"] == {"expected_version": 1, "actual_version": 2}
    assert illegal.status_code == 409
    assert illegal.json()["error_code"] == "invalid_incident_transition"
    assert illegal.json()["details"]["status"] == "investigating"


async def test_event_list_supports_forward_cursor_replay(
    control_client: httpx2.AsyncClient,
) -> None:
    created = await control_client.post(
        "/api/v1/alerts",
        json=alert_payload("alert-001"),
    )
    incident_id = created.json()["incident"]["id"]
    await control_client.post(
        f"/api/v1/incidents/{incident_id}/actions/start-investigation",
        json={
            "expected_version": 1,
            "actor_id": "engineer-001",
            "reason": "开始调查",
        },
    )

    first_page = await control_client.get(
        f"/api/v1/incidents/{incident_id}/events",
        params={"limit": 1},
    )
    first_cursor = first_page.json()["next_after_cursor"]
    second_page = await control_client.get(
        f"/api/v1/incidents/{incident_id}/events",
        params={"after_cursor": first_cursor, "limit": 1},
    )

    assert first_page.json()["items"][0]["event_type"] == "incident.created"
    assert isinstance(first_cursor, int)
    assert second_page.json()["items"][0]["event_type"] == "incident.status_changed"
    assert second_page.json()["items"][0]["actor_id"] == "engineer-001"
    assert second_page.json()["items"][0]["reason"] == "开始调查"
    assert second_page.json()["next_after_cursor"] is None


async def test_missing_incident_returns_structured_404(
    control_client: httpx2.AsyncClient,
) -> None:
    incident_id = uuid4()

    response = await control_client.get(f"/api/v1/incidents/{incident_id}")

    assert response.status_code == 404
    assert response.json() == {
        "error_code": "incident_not_found",
        "message": f"Incident 不存在：{incident_id}",
        "details": {"incident_id": str(incident_id)},
    }


async def test_request_validation_rejects_unknown_fields_and_naive_time(
    control_client: httpx2.AsyncClient,
) -> None:
    unknown_field_payload = alert_payload("alert-001")
    unknown_field_payload["ingested_at"] = "2026-01-01T08:00:00Z"
    naive_time_payload = alert_payload("alert-002")
    naive_time_payload["occurred_at"] = "2026-01-01T07:59:59"

    unknown_field = await control_client.post(
        "/api/v1/alerts",
        json=unknown_field_payload,
    )
    naive_time = await control_client.post(
        "/api/v1/alerts",
        json=naive_time_payload,
    )

    assert unknown_field.status_code == 422
    assert unknown_field.json()["error_code"] == "request_validation_error"
    assert unknown_field.json()["details"]["issues"][0]["location"] == "body.ingested_at"
    assert naive_time.status_code == 422
    assert naive_time.json()["error_code"] == "request_validation_error"


async def test_domain_payload_limit_maps_to_structured_422(
    control_client: httpx2.AsyncClient,
) -> None:
    payload = alert_payload("alert-001")
    payload["payload"] = {"content": "故" * (64 * 1024)}

    response = await control_client.post("/api/v1/alerts", json=payload)

    assert response.status_code == 422
    assert response.json()["error_code"] == "incident_validation_error"
