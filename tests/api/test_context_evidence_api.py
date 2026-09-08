"""真实 PostgreSQL 下的目录、单条读取、隔离与不可变引用。"""

from uuid import uuid4

import httpx2
import pytest
from sqlalchemy import update

from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.tasks.evidence import evidence_hash
from opsswarm.investigation.tasks.evidence_tables import AgentTaskEvidenceRow
from opsswarm.investigation.tasks.queries import CollaborationQueries
from tests.integration.test_context_reservations import prepared_context, reserve_context

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.mark.parametrize("case", ["normal", "corrupt", "replaced", "other_run"])
async def test_catalog_and_detail_preserve_call_scope_and_read_only_facts(
    database_session_factory,
    migrated_database_url,
    case,
):
    db = database_session_factory
    prepared, packet, store = await prepared_context(db)
    grant = await reserve_context(store, prepared, packet)
    run_id, task_id = prepared[1].run.id, prepared[4].id
    budget, tasks = CallBudgetQueries(db), CollaborationQueries(db)
    before_budget = await budget.get_snapshot(run_id)
    before_tasks = await tasks.get_snapshot(run_id)
    evidence = await tasks.get_evidence(run_id, task_id)
    root = f"/api/v1/investigations/{run_id}/calls/{grant.record.id}"
    app = create_app(database_settings=DatabaseSettings(database_url=migrated_database_url))
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        response = await client.get(root + "/evidence-catalog")
        assert response.status_code == 200
        group = response.json()["groups"][0]
        assert group["task_id"] == str(task_id)
        assert group["snapshot_sha256"] == evidence.content_sha256
        assert "attributes" not in response.text and "lease_token" not in response.text
        item = group["entries"][0]
        detail_url = root + f"/evidence/{task_id}/{item['evidence_id']}"
        params = {"snapshot_sha256": group["snapshot_sha256"]}
        if case in ("corrupt", "replaced"):
            values = {"content_sha256": "a" * 64}
            if case == "replaced":
                observations = list(evidence.snapshot.evidence)
                observations[0] = observations[0].model_copy(update={"summary": "存储发生变化"})
                changed = evidence.snapshot.model_copy(update={"evidence": tuple(observations)})
                values = {
                    "snapshot": changed.model_dump(mode="json"),
                    "content_sha256": evidence_hash(changed),
                }
            async with db() as session, session.begin():
                await session.execute(
                    update(AgentTaskEvidenceRow)
                    .where(
                        AgentTaskEvidenceRow.task_id == task_id,
                    )
                    .values(**values)
                )
            for url in (root + "/evidence-catalog", detail_url):
                failure = await client.get(url, params=params)
                assert failure.status_code == 409
                assert failure.json()["error_code"] == "context_evidence_corrupt"
        elif case == "other_run":
            other, _, _ = await prepared_context(db)
            wrong = root.replace(str(run_id), str(other[1].run.id))
            assert (await client.get(wrong + "/evidence-catalog")).status_code == 404
            # 同事故内的其他专业任务也不能通过日志调用读取。
            for other_id in (
                other[4].id,
                next(t.id for t in before_tasks.rounds[0].tasks if t.id != task_id),
            ):
                failure = await client.get(
                    root + f"/evidence/{other_id}/{item['evidence_id']}", params=params
                )
                assert failure.status_code == 404
        else:
            detail = await client.get(detail_url, params=params)
            expected = next(
                e for e in evidence.snapshot.evidence if e.evidence_id == item["evidence_id"]
            )
            assert detail.status_code == 200 and detail.json()["evidence"] == expected.model_dump(
                mode="json"
            )
            assert (
                await client.get(detail_url, params={"snapshot_sha256": "a" * 64})
            ).status_code == 409
            for invalid in ({}, {"snapshot_sha256": "bad"}):
                assert (await client.get(detail_url, params=invalid)).status_code == 422
            assert (
                await client.get(root + f"/evidence/{task_id}/missing", params=params)
            ).status_code == 404
            assert (
                await client.get(root + f"/evidence/{task_id}/INVALID", params=params)
            ).status_code == 422
            tool_call = next(c for c in before_budget.calls if c.kind == "tool")
            legacy_root = root.replace(str(grant.record.id), str(tool_call.id))
            assert (await client.get(legacy_root + "/evidence-catalog")).json() is None
            assert (
                await client.get(
                    legacy_root + f"/evidence/{task_id}/{item['evidence_id']}", params=params
                )
            ).status_code == 409
            for url in (root + "/evidence-catalog", detail_url):
                assert (await client.post(url, json={})).status_code == 405
            assert (
                await client.get(
                    root.replace(str(grant.record.id), str(uuid4())) + "/evidence-catalog"
                )
            ).status_code == 404
        assert await budget.get_snapshot(run_id) == before_budget
        assert await tasks.get_snapshot(run_id) == before_tasks
        assert await budget.get_context_manifest(run_id, grant.record.id) == packet.manifest
    for suffix in ("/evidence-catalog", "/evidence/{task_id}/{evidence_id}"):
        spec = app.openapi()["paths"]["/api/v1/investigations/{run_id}/calls/{call_id}" + suffix]
        assert set(spec) == {"get"} and set(spec["get"]["responses"]) >= {
            "200",
            "404",
            "409",
            "422",
        }
