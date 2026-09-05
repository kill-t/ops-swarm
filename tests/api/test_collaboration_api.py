"""专业任务只读 API 的真实 PostgreSQL 验证。"""

from uuid import uuid4

import httpx2
import pytest

from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from tests.task_facts_helpers import TaskClock, assignment, claimed_run

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_read_empty_and_persisted_task_facts(
    migrated_database_url,
    database_session_factory,
):
    _, claim = await claimed_run(database_session_factory)
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    url = f"/api/v1/investigations/{claim.run.id}/collaboration"
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        initial = await client.get(url)
        assert initial.status_code == 200
        assert initial.json() == {"investigation_run_id": str(claim.run.id), "rounds": []}
        store = InvestigationTaskStore(database_session_factory, clock=TaskClock())
        await store.create_round(
            claim.run.id,
            lease_token=claim.work_item.lease_token,
            assignment=assignment(count=3),
        )
        snapshot = await CollaborationQueries(database_session_factory).get_snapshot(claim.run.id)
        task = snapshot.rounds[0].tasks[0]
        await store.begin_attempt(
            claim.run.id, task.id, lease_token=claim.work_item.lease_token, expected_version=1
        )
        response = await client.get(url)
        assert response.status_code == 200
        body = response.json()
        tasks = body["rounds"][0]["tasks"]
        assert [item["assignment"]["task_key"] for item in tasks] == ["change", "log", "metric"]
        attempt = tasks[0]["attempts"][0]
        assert attempt["investigation_attempt_id"] == str(claim.attempt.id)
        assert attempt["status"] == "running" and attempt["outcome"] is None
        assert "lease_token" not in response.text
        assert str(claim.work_item.lease_token) not in response.text
        assert (await client.get(url)).json() == body
        assert (await client.post(url, json={})).status_code == 405


async def test_unknown_and_malformed_run_use_standard_errors(migrated_database_url):
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        for value, status, code in (
            (str(uuid4()), 404, "investigation_run_not_found"),
            ("invalid", 422, "request_validation_error"),
        ):
            response = await client.get(f"/api/v1/investigations/{value}/collaboration")
            assert response.status_code == status
            assert response.json()["error_code"] == code
    path = app.openapi()["paths"]["/api/v1/investigations/{run_id}/collaboration"]
    assert set(path) == {"get"}
    assert set(path["get"]["responses"]) >= {"200", "404", "422"}
