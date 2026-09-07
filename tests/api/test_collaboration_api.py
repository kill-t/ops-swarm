"""专业任务只读 API 的真实 PostgreSQL 验证。"""

from uuid import uuid4

import httpx2
import pytest

from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.incidents.persistence import IncidentStore
from opsswarm.investigation.tasks import api as task_api
from opsswarm.investigation.tasks.queries import CollaborationQueries
from opsswarm.investigation.tasks.store import InvestigationTaskStore
from opsswarm.investigation.tasks.stream import stream_task_events
from tests.integration.test_incident_store import make_submission
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


async def test_versioned_start_and_invalid_routes_are_explicit(
    migrated_database_url,
    database_session_factory,
):
    incident = await IncidentStore(database_session_factory).ingest_alert(make_submission("v2-api"))
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    url = f"/api/v1/incidents/{incident.incident.id}/actions/start-investigation"
    body = {
        "expected_version": 1,
        "actor_id": "engineer",
        "reason": "启动固定任务图",
        "workflow_version": "v2",
        "mode": "multi_agent",
    }
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        for overrides in (
            {"mode": "single_agent"},
            {"observation_cutoff_at": "2026-09-07T00:00:00Z"},
            {"workflow_version": "v9"},
            {"evidence_policy_version": "task-evidence-v1"},
        ):
            rejected = await client.post(url, json={**body, **overrides})
            assert rejected.status_code == 422
            assert rejected.json()["error_code"] == "request_validation_error"
        response = await client.post(url, json=body)
        assert response.status_code == 200
        run = response.json()["investigation"]
        assert run["workflow_version"] == "v2"
        assert run["evidence_policy_version"] == "task-evidence-v1"
        assert (await client.get(f"/api/v1/investigations/{run['id']}")).json()[
            "workflow_version"
        ] == "v2"
        assert (await client.post(url, json=body)).status_code == 409


async def test_result_and_event_routes_replay_only_committed_records(
    migrated_database_url,
    database_session_factory,
    monkeypatch,
):
    _, claim = await claimed_run(database_session_factory)
    tasks = InvestigationTaskStore(database_session_factory, clock=TaskClock())
    queries = CollaborationQueries(database_session_factory)
    await tasks.create_round(
        claim.run.id, lease_token=claim.work_item.lease_token, assignment=assignment()
    )
    task = (await queries.get_snapshot(claim.run.id)).rounds[0].tasks[0]
    await tasks.begin_attempt(
        claim.run.id, task.id, lease_token=claim.work_item.lease_token, expected_version=1
    )

    async def one_frame(queries, run_id, **kwargs):
        stream = stream_task_events(queries, run_id, **kwargs)
        try:
            yield await anext(stream)
        finally:
            await stream.aclose()

    monkeypatch.setattr(task_api, "stream_task_events", one_frame)
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    base = f"/api/v1/investigations/{claim.run.id}/collaboration"
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        assert (await client.get(f"{base}/tasks/{task.id}/result")).json() is None
        missing = await client.get(f"{base}/tasks/{uuid4()}/result")
        assert missing.status_code == 404 and missing.json()["error_code"] == "task_not_found"
        events = (await client.get(f"{base}/events", params={"after_cursor": 1, "limit": 1})).json()
        assert len(events) == 1 and events[0]["cursor"] == 2
        assert str(claim.work_item.lease_token) not in str(events)
        assert (await client.get(f"{base}/events", params={"limit": 101})).status_code == 422
        assert (await client.get(f"{base}/events", params={"after_cursor": -1})).status_code == 422
        replay = await client.get(f"{base}/events/stream", headers={"Last-Event-ID": "1"})
        assert replay.status_code == 200 and replay.headers["content-type"].startswith(
            "text/event-stream"
        )
        assert replay.text.startswith("id: 2\nevent: task.started\n")
        assert "id: 1\n" not in replay.text
        assert (
            await client.get(f"{base}/events/stream", headers={"Last-Event-ID": "-1"})
        ).status_code == 422
        unknown = f"/api/v1/investigations/{uuid4()}/collaboration/events/stream"
        assert (await client.get(unknown)).status_code == 404
