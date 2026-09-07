"""取证快照在报告前可读，空结果与尚未采集有明确区别。"""

from uuid import uuid4

import httpx2
import pytest

from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from tests.task_evidence_helpers import evidence_attempt, material, save_evidence

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("empty", [False, True])
async def test_read_snapshot_before_report_and_reject_wrong_scope(
    migrated_database_url, database_session_factory, empty
):
    prepared = await evidence_attempt(database_session_factory)
    _, claim, _, _, task, _, _ = prepared
    second = await evidence_attempt(database_session_factory)
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    base = f"/api/v1/investigations/{claim.run.id}/collaboration"
    url = f"{base}/tasks/{task.id}/evidence"
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        initial = await client.get(url)
        assert initial.status_code == 200 and initial.json() is None
        record = await save_evidence(prepared, () if empty else material())
        response = await client.get(url)
        assert response.status_code == 200
        assert response.json() == record.model_dump(mode="json")
        assert (await client.get(url)).json() == response.json()
        assert str(claim.work_item.lease_token) not in response.text
        assert (await client.get(f"{base}/tasks/{task.id}/result")).json() is None
        run = (await client.get(f"/api/v1/investigations/{claim.run.id}")).json()
        assert run["evidence_policy_version"] == "task-evidence-v1"
        assert "content_sha256" not in (await client.get(base)).text
        assert (await client.post(url, json={})).status_code == 405
        for bad_url, code in (
            (f"{base}/tasks/{second[4].id}/evidence", "task_not_found"),
            (
                f"/api/v1/investigations/{uuid4()}/collaboration/tasks/{task.id}/evidence",
                "investigation_run_not_found",
            ),
        ):
            missing = await client.get(bad_url)
            assert missing.status_code == 404 and missing.json()["error_code"] == code
        assert (await client.get(f"{base}/tasks/invalid/evidence")).status_code == 422
    path = app.openapi()["paths"][
        "/api/v1/investigations/{run_id}/collaboration/tasks/{task_id}/evidence"
    ]
    assert set(path) == {"get"}
