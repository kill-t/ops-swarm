"""预算配置、历史未知语义与只读账本 API。"""

from uuid import uuid4

import httpx2
import pytest

from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.incidents.persistence import IncidentStore
from tests.integration.test_incident_store import make_submission
from tests.task_facts_helpers import started_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_call_budget_exposes_server_assigned_slots(
    migrated_database_url, database_session_factory
):
    from tests.integration.test_call_budget_store import prepare
    from tests.integration.test_repeated_calls import reserve_slot

    _, claim, _, _, attempts, store = await prepare(database_session_factory)
    for index in (0, 1):
        await reserve_slot(store, claim, attempts["log"], index)
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        url = f"/api/v1/investigations/{claim.run.id}/call-budget"
        response = await client.get(url)
        assert response.status_code == 200
        budget = response.json()
        assert [c["invocation_index"] for c in budget["calls"]] == [0, 1]
        assert budget["reserved_tool_calls"] == 2
        assert "lease_token" not in str(budget) and "idempotency_key" not in str(budget)
        assert (await client.post(url, json={"invocation_index": 2})).status_code == 405
        assert (await client.get(url)).json() == budget


async def test_start_pins_policy_and_budget_history_is_read_only(
    migrated_database_url, database_session_factory
):
    incident = await IncidentStore(database_session_factory).ingest_alert(
        make_submission("budget-api")
    )
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    body = {
        "expected_version": 1,
        "actor_id": "engineer",
        "reason": "预算入口测试",
        "mode": "multi_agent",
        "workflow_version": "v2",
    }
    url = f"/api/v1/incidents/{incident.incident.id}/actions/start-investigation"
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        for changed in (
            {"call_budget": {"max_model_calls": True}},
            {"call_budget": {"max_tool_calls": 101}},
            {"call_budget": {"version": "unsupported"}},
            {"workflow_version": "v1", "call_budget": {"max_model_calls": 1}},
        ):
            response = await client.post(url, json={**body, **changed})
            assert (
                response.status_code == 422
                and response.json()["error_code"] == "request_validation_error"
            )
        response = await client.post(url, json={**body, "call_budget": {"max_model_calls": 5}})
        assert response.status_code == 200
        run = response.json()["investigation"]
        assert run["call_budget"]["max_model_calls"] == 5
        budget_url = f"/api/v1/investigations/{run['id']}/call-budget"
        budget = (await client.get(budget_url)).json()
        assert budget["policy"] == run["call_budget"]
        assert budget["started_at"] is None and budget["deadline"] is None
        assert budget["reserved_model_calls"] == 0 and budget["reserved_tool_calls"] == 0
        assert budget["calls"] == [] and "lease_token" not in str(budget)
        assert (await client.post(budget_url, json={"max_model_calls": 60})).status_code == 405
        assert (await client.get(budget_url)).json() == budget


async def test_legacy_budget_is_unknown_and_errors_use_existing_contract(
    migrated_database_url, database_session_factory
):
    _, legacy = await started_run(database_session_factory)
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        budget = (await client.get(f"/api/v1/investigations/{legacy.run.id}/call-budget")).json()
        assert (
            budget["policy"] is None
            and budget["reserved_model_calls"] is None
            and budget["reserved_tool_calls"] is None
        )
        assert budget["calls"] == []
        for value, code, status in (
            (str(uuid4()), "investigation_run_not_found", 404),
            ("bad-id", "request_validation_error", 422),
        ):
            response = await client.get(f"/api/v1/investigations/{value}/call-budget")
            assert response.status_code == status and response.json()["error_code"] == code
    spec = app.openapi()["paths"]["/api/v1/investigations/{run_id}/call-budget"]
    assert set(spec) == {"get"}
    assert set(spec["get"]["responses"]) >= {"200", "404", "422"}


@pytest.mark.parametrize("policy_fields", [{}, {"call_budget": None}])
async def test_missing_or_null_v2_policy_pins_defaults(
    migrated_database_url, database_session_factory, policy_fields
):
    incident = await IncidentStore(database_session_factory).ingest_alert(
        make_submission("default-budget-api")
    )
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        response = await client.post(
            f"/api/v1/incidents/{incident.incident.id}/actions/start-investigation",
            json={
                "expected_version": 1,
                "actor_id": "engineer",
                "reason": "新运行不能通过空值跳过预算",
                "mode": "multi_agent",
                "workflow_version": "v2",
                **policy_fields,
            },
        )
        assert response.status_code == 200
        assert response.json()["investigation"]["call_budget"] == {
            "version": "call-count-v1",
            "max_model_calls": 12,
            "max_tool_calls": 18,
            "max_tool_calls_per_task": 6,
            "max_duration_seconds": 300,
        }
