"""工作摘要只读接口区分缺少材料、越权任务和来源冲突。"""

from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import update

from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.investigation.tasks.artifact_tables import AgentTaskResultRow
from tests.task_facts_helpers import started_run
from tests.working_summary_helpers import analyzed_task, finish

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def test_summary_read_api_and_immutable_methods(
    migrated_database_url, database_session_factory
):
    db = database_session_factory
    prepared, result = await analyzed_task(db)
    _, claim, _, _, task, _, _ = prepared
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    url = f"/api/v1/investigations/{claim.run.id}/collaboration/tasks/{task.id}/working-summary"
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        before = await client.get(url)
        assert before.status_code == 200 and before.json() is None
        await finish(prepared, result)
        response = await client.get(url)
        assert response.status_code == 200
        body = response.json()
        assert body["summary"]["report"] == result.report.model_dump(mode="json")
        assert body["version"] == "task-working-summary-v1"
        assert (await client.get(url)).json() == body
        assert (await client.get(url.replace(str(task.id), str(uuid4())))).status_code == 404
        _, other = await started_run(db)
        assert (
            await client.get(url.replace(str(claim.run.id), str(other.run.id)))
        ).status_code == 404
        assert (await client.get(url.replace(str(claim.run.id), str(uuid4())))).status_code == 404
        assert (await client.post(url, json=body)).status_code == 405
        assert (await client.delete(url)).status_code == 405
        run = await client.get(f"/api/v1/investigations/{claim.run.id}")
        assert run.status_code == 200
        assert run.json()["working_summary_policy_version"] == "task-working-summary-v1"
        async with db() as session, session.begin():
            await session.execute(
                update(AgentTaskResultRow)
                .where(AgentTaskResultRow.task_id == task.id)
                .values(
                    result=result.model_copy(
                        update={"report": result.report.model_copy(update={"confidence": 0.2})}
                    ).model_dump(mode="json")
                )
            )
        invalid = await client.get(url)
        assert invalid.status_code == 409
        assert invalid.json()["error_code"] == "working_summary_report_mismatch"
