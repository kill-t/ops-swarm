"""变更记录模拟服务 API 集成测试。"""

from datetime import UTC, datetime

import httpx2
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from opsswarm.change_records.app import create_app

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
NOW = datetime(2026, 8, 30, 12, tzinfo=UTC)


async def test_admin_write_is_idempotent_and_read_api_returns_record(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(database_session_factory, clock=lambda: NOW)
    documented_responses = app.openapi()["paths"]["/__admin/changes"]["post"]["responses"]
    assert {"200", "201", "409", "422"} <= set(documented_responses)
    payload = {
        "change_type": "deployment",
        "external_change_id": "release-rc1",
        "from_version": "stable",
        "to_version": "2026.01.01-rc1",
        "completed": True,
    }
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        created = await client.post("/__admin/changes", json=payload)
        duplicate = await client.post("/__admin/changes", json=payload)
        listed = await client.get(
            "/api/v1/changes",
            params={"service_name": "order-service", "lookback_hours": 6},
        )

    assert created.status_code == 201
    assert duplicate.status_code == 200
    assert duplicate.json()["deduplicated"] is True
    assert duplicate.json()["record"] == created.json()["record"]
    assert listed.status_code == 200
    assert listed.json()["generated_at"] == "2026-08-30T12:00:00Z"
    assert listed.json()["items"] == [created.json()["record"]]
    assert listed.json()["truncated"] is False


async def test_same_external_id_with_different_content_returns_409(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(database_session_factory, clock=lambda: NOW)
    payload = {
        "change_type": "deployment",
        "external_change_id": "release-rc1",
        "from_version": "stable",
        "to_version": "rc1",
    }
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        assert (await client.post("/__admin/changes", json=payload)).status_code == 201
        payload["to_version"] = "rc2"
        conflict = await client.post("/__admin/changes", json=payload)

    assert conflict.status_code == 409
    assert conflict.json() == {
        "error_code": "change_record_idempotency_conflict",
        "message": "外部变更号已存在但内容不同：release-rc1",
        "details": {"external_change_id": "release-rc1"},
    }


async def test_api_rejects_unknown_fields_backdating_and_unbounded_queries(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    app = create_app(database_session_factory, clock=lambda: NOW)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        invalid_write = await client.post(
            "/__admin/changes",
            json={
                "change_type": "dependency_maintenance",
                "external_change_id": "redis-1",
                "occurred_at": "2020-01-01T00:00:00Z",
            },
        )
        invalid_service = await client.get(
            "/api/v1/changes",
            params={"service_name": "payment-service", "lookback_hours": 6},
        )
        invalid_window = await client.get(
            "/api/v1/changes",
            params={"service_name": "order-service", "lookback_hours": 25},
        )

    assert invalid_write.status_code == 422
    assert invalid_service.status_code == 422
    assert invalid_window.status_code == 422
