"""现场 v2 创建的服务端窗口及协作材料只读 API。"""

from datetime import datetime, timedelta

import httpx2
import pytest

from opsswarm.checkpoints import initialize_checkpoint_schema
from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.persistence import InvestigationStore
from tests.integration.test_incident_store import make_submission
from tests.integration.test_live_task_graph import run_live_worker
from tests.live_task_helpers import LiveHttpFixture
from tests.task_facts_helpers import TaskClock

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_live_v2_window_is_server_owned_and_evidence_is_readable(
    migrated_database_url, database_session_factory, checkpoint_database_url
):
    await initialize_checkpoint_schema(checkpoint_database_url)
    incident = await IncidentStore(database_session_factory).ingest_alert(
        make_submission("live-v2-api")
    )
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        url = f"/api/v1/incidents/{incident.incident.id}/actions/start-investigation"
        body = {
            "expected_version": 1,
            "actor_id": "engineer",
            "reason": "固定现场窗口",
            "mode": "multi_agent",
            "data_source_mode": "live",
            "workflow_version": "v2",
        }
        for extra in (
            {"observation_cutoff_at": "2026-09-07T00:00:00Z"},
            {"evidence_policy_version": "task-evidence-live-v1"},
            {"mode": "single_agent"},
        ):
            assert (await client.post(url, json={**body, **extra})).status_code == 422
        response = await client.post(url, json=body)
        assert response.status_code == 200
        run = response.json()["investigation"]
        cutoff = datetime.fromisoformat(run["observation_cutoff_at"])
        assert cutoff == datetime.fromisoformat(run["created_at"]).replace(microsecond=0)
        assert run["evidence_policy_version"] == "task-evidence-live-mcp-v1" and run["call_budget"]
        run_url = f"/api/v1/investigations/{run['id']}"
        assert (await client.post(url, json=body)).status_code == 409
        assert (await client.get(run_url)).json() == run
        clock = TaskClock(cutoff + timedelta(seconds=2))
        fixture = LiveHttpFixture(cutoff, clock)
        assert await run_live_worker(
            database_session_factory,
            checkpoint_database_url,
            InvestigationStore(database_session_factory),
            clock,
            fixture.source,
        )
        collaboration = (await client.get(f"{run_url}/collaboration")).json()
        for task in collaboration["rounds"][0]["tasks"]:
            evidence_url = f"{run_url}/collaboration/tasks/{task['id']}/evidence"
            saved = (await client.get(evidence_url)).json()
            assert saved["snapshot"]["data_source_mode"] == "live"
            assert saved["snapshot"]["window_end"] == run["observation_cutoff_at"]
            assert len(saved["content_sha256"]) == 64
            assert (await client.post(evidence_url, json=saved)).status_code == 405
        assert (await client.get(run_url)).json()["metrics"] is None
