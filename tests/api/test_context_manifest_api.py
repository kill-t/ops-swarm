"""上下文清单只读查询、运行隔离与历史空值语义。"""

from uuid import uuid4

import httpx2
import pytest

from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.investigation.budget.queries import CallBudgetQueries
from tests.integration.test_call_budget_store import prepare
from tests.integration.test_context_reservations import prepared_context, reserve_context
from tests.integration.test_model_budget_store import reserve_model
from tests.model_budget_helpers import model_policy

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_context_manifest_api_is_scoped_read_only_and_compatible(
    database_session_factory,
    migrated_database_url,
):
    prepared, packet, store = await prepared_context(database_session_factory)
    grant = await reserve_context(store, prepared, packet)
    run_id = prepared[1].run.id
    _, legacy, _, _, attempts, legacy_store = await prepare(
        database_session_factory, model_budget=model_policy()
    )
    old_call = await reserve_model(legacy_store, legacy, attempts["log"])
    calls = await CallBudgetQueries(database_session_factory).get_snapshot(run_id)
    tool_call = next(c for c in calls.calls if c.kind == "tool")
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    path = f"/api/v1/investigations/{run_id}/calls/{grant.record.id}/context-manifest"
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        response = await client.get(path)
        assert response.status_code == 200 and response.json() == packet.manifest.model_dump(
            mode="json"
        )
        assert "lease_token" not in response.text
        assert (await client.post(path, json={})).status_code == 405
        assert (await client.get(path)).json() == response.json()
        for scoped_run, call_id in ((legacy.run.id, old_call.record.id), (run_id, tool_call.id)):
            response = await client.get(
                f"/api/v1/investigations/{scoped_run}/calls/{call_id}/context-manifest"
            )
            assert response.status_code == 200 and response.json() is None
        for scoped_run, call_id, expected in (
            (legacy.run.id, grant.record.id, 404),
            (run_id, uuid4(), 404),
            (uuid4(), grant.record.id, 404),
            (run_id, "bad-id", 422),
        ):
            response = await client.get(
                f"/api/v1/investigations/{scoped_run}/calls/{call_id}/context-manifest"
            )
            assert response.status_code == expected and "error_code" in response.json()
        run = (await client.get(f"/api/v1/investigations/{run_id}")).json()
        assert run["context_policy_version"] == "task-context-v1"
    spec = app.openapi()["paths"][
        "/api/v1/investigations/{run_id}/calls/{call_id}/context-manifest"
    ]
    assert set(spec) == {"get"} and set(spec["get"]["responses"]) >= {"200", "404", "422"}
