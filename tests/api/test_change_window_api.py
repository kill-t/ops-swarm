"""真实 PostgreSQL 下历史事件和记录截止的独立过滤。"""

from datetime import timedelta

import httpx2
import pytest

from opsswarm.change_records.app import create_app
from opsswarm.change_records.models import ConfigurationChange
from opsswarm.change_records.persistence import ChangeRecordStore
from tests.api.test_change_record_api import NOW

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
START = NOW - timedelta(hours=1)


async def test_window_filters_late_record_before_limit_and_legacy_stays_compatible(
    database_session_factory,
):
    store = ChangeRecordStore(database_session_factory)
    for identity, occurred, recorded in (
        ("start", START, START),
        ("end", NOW, NOW),
        ("older", START - timedelta(seconds=1), START),
        ("newer", NOW + timedelta(seconds=1), NOW + timedelta(seconds=1)),
        ("late", NOW - timedelta(seconds=10), NOW + timedelta(seconds=1)),
    ):
        await store.record(
            ConfigurationChange(external_change_id=identity, from_value=50, to_value=5),
            occurred_at=occurred,
            recorded_at=recorded,
        )
    current = NOW + timedelta(hours=1)
    app = create_app(database_session_factory, clock=lambda: current)
    params = {
        "service_name": "order-service",
        "window_start": START.isoformat(),
        "window_end": NOW.isoformat(),
    }
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        first = await client.get("/api/v1/changes/window", params=params)
        assert first.status_code == 200
        body = first.json()
        assert [item["change"]["external_change_id"] for item in body["items"]] == ["end", "start"]
        assert body["window"]["window_end"] == NOW.isoformat().replace("+00:00", "Z")
        current += timedelta(hours=1)
        second = (await client.get("/api/v1/changes/window", params=params)).json()
        assert second["items"] == body["items"] and second["window"] == body["window"]
        assert second["generated_at"] != body["generated_at"]
        legacy = (
            await client.get("/api/v1/changes", params={"service_name": "order-service"})
        ).json()
        assert len(legacy["items"]) == 5 and "window" not in legacy
        next_window = (
            await client.get(
                "/api/v1/changes/window",
                params={
                    **params,
                    "window_end": (NOW + timedelta(seconds=1)).isoformat(),
                },
            )
        ).json()
        assert {e["change"]["external_change_id"] for e in next_window["items"]} == {
            "start",
            "end",
            "late",
            "newer",
        }
        assert (await client.post("/api/v1/changes/window", json=params)).status_code == 405
    limited = await store.list_recent(
        service_name="order-service",
        since=START,
        until=NOW,
        recorded_until=NOW - timedelta(seconds=1),
        limit=1,
    )
    assert limited.items[0].change.external_change_id == "start" and not limited.truncated


@pytest.mark.parametrize(
    "invalid", ["future", "naive", "reversed", "large", "fraction", "service", "extra", "missing"]
)
async def test_invalid_query_rejected_before_repository(
    database_session_factory, monkeypatch, invalid
):
    async def reject_call(*args, **kwargs):
        raise AssertionError("非法窗口不得访问仓储")

    monkeypatch.setattr(ChangeRecordStore, "list_recent", reject_call)
    app = create_app(database_session_factory, clock=lambda: NOW)
    params = {
        "service_name": "order-service",
        "window_start": START.isoformat(),
        "window_end": NOW.isoformat(),
    }
    if invalid == "future":
        params["window_end"] = (NOW + timedelta(seconds=1)).isoformat()
    elif invalid == "naive":
        params["window_end"] = NOW.replace(tzinfo=None).isoformat()
    elif invalid == "reversed":
        params["window_start"] = (NOW + timedelta(seconds=1)).isoformat()
    elif invalid == "large":
        params["window_start"] = (NOW - timedelta(days=2)).isoformat()
    elif invalid == "fraction":
        params["window_end"] = (NOW - timedelta(microseconds=1)).isoformat()
    elif invalid == "service":
        params["service_name"] = "another-service"
    elif invalid == "extra":
        params["lookback_hours"] = "6"
    else:
        params.pop("window_end")
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get("/api/v1/changes/window", params=params)
    assert response.status_code == 422


async def test_empty_window_response_echoes_original_query(database_session_factory):
    app = create_app(database_session_factory, clock=lambda: NOW)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://test"
    ) as client:
        response = await client.get(
            "/api/v1/changes/window",
            params={
                "service_name": "order-service",
                "window_start": START.isoformat(),
                "window_end": NOW.isoformat(),
            },
        )
    assert response.status_code == 200
    assert response.json()["items"] == [] and response.json()["truncated"] is False
    assert response.json()["window"]["window_start"] == START.isoformat().replace("+00:00", "Z")
