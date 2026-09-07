"""显式模型预算配置与按调用读取输入的 API 边界。"""

from uuid import uuid4

import httpx2
import pytest

from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.incidents.persistence import IncidentStore
from tests.integration.test_call_budget_store import prepare
from tests.integration.test_incident_store import make_submission
from tests.integration.test_model_budget_store import reserve_model
from tests.model_budget_helpers import model_input, model_policy

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_start_pins_model_profile_and_rejects_invalid_configuration(
    migrated_database_url, database_session_factory
):
    incident = await IncidentStore(database_session_factory).ingest_alert(
        make_submission("model-budget-api")
    )
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    policy = model_policy().model_dump(mode="json")
    body = {
        "expected_version": 1,
        "actor_id": "engineer",
        "reason": "显式预算测试",
        "mode": "multi_agent",
        "workflow_version": "v2",
        "model_budget": policy,
    }
    url = f"/api/v1/incidents/{incident.incident.id}/actions/start-investigation"
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        for changed in (
            {"workflow_version": "v1"},
            {"context_policy_version": None},
            {"model_budget": {**policy, "max_input_tokens": True}},
            {"model_budget": {**policy, "max_output_tokens_per_call": 0}},
            {"model_budget": {**policy, "max_estimated_cost_usd": "101"}},
            {"model_budget": {**policy, "api_key": "禁止把密钥写进预算"}},
        ):
            response = await client.post(url, json={**body, **changed})
            assert (
                response.status_code == 422
                and response.json()["error_code"] == "request_validation_error"
            )
        response = await client.post(url, json=body)
        assert response.status_code == 200
        run = response.json()["investigation"]
        assert run["model_budget"] == policy and run["call_budget"] is not None
        assert run["context_policy_version"] == "task-context-v1"
        assert run["prompt_version"] == "fixed-task-context-v1"
        budget = (await client.get(f"/api/v1/investigations/{run['id']}/call-budget")).json()
        assert budget["model_budget"] == policy
        assert budget["model_usage"]["committed_input_tokens"] == 0


async def test_inputs_are_read_only_scoped_and_not_in_budget_list(
    migrated_database_url, database_session_factory
):
    _, claim, _, _, attempts, store = await prepare(
        database_session_factory, model_budget=model_policy()
    )
    grant = await reserve_model(store, claim, attempts["log"])
    _, other, _, _, _, _ = await prepare(database_session_factory, model_budget=model_policy())
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        url = f"/api/v1/investigations/{claim.run.id}/calls/{grant.record.id}/model-input"
        response = await client.get(url)
        assert response.status_code == 200 and response.json() == model_input().model_dump(
            mode="json"
        )
        assert (await client.post(url, json={})).status_code == 405
        budget = (await client.get(f"/api/v1/investigations/{claim.run.id}/call-budget")).json()
        assert "messages" not in str(budget) and "lease_token" not in str(budget)
        assert budget["calls"][0]["model_usage"] is None
        assert budget["model_usage"]["reported_cost_usd"] is None
        for run_id, call_id, status in (
            (other.run.id, grant.record.id, 404),
            (claim.run.id, uuid4(), 404),
            (uuid4(), grant.record.id, 404),
            ("bad-id", grant.record.id, 422),
        ):
            response = await client.get(
                f"/api/v1/investigations/{run_id}/calls/{call_id}/model-input"
            )
            assert response.status_code == status
            assert "error_code" in response.json()
