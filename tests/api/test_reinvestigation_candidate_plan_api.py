"""同一新候选来源上的可信草案准入、原生保护及历史重放。"""

import asyncio
import json
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from alembic import command
from opsswarm.approval.conditions.queries import raw_content_hash
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.reinvestigation_candidate.job_store import ReflowCandidateJobStore
from opsswarm.investigation.reinvestigation_candidate.ledger import ReflowCandidateLedger
from opsswarm.investigation.reinvestigation_candidate.queries import ReflowCandidateQueries
from opsswarm.planning.reinvestigation_candidate.store import ReflowPlanStore
from opsswarm.planning.reinvestigation_candidate.tables import ReflowPlanRow
from tests.api import test_reinvestigation_candidate_api as candidate
from tests.api.test_reinvestigation_candidate_api import (
    current_fixture_time as current_fixture_time,
)
from tests.api.test_reinvestigation_candidate_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_candidate_api import wait_context as wait_context
from tests.identity_helpers import auth_header
from tests.integration.test_candidate_approval_migration import config

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def ready(c, monkeypatch):
    execution = candidate.routing.review.synthesis.execution
    original = execution.observation

    def observation(assignment, window, **kwargs):
        item = original(assignment, window, **kwargs)
        return item.model_copy(
            update={
                "attributes": item.attributes
                | {
                    "change_type": "deployment",
                    "completed": True,
                    "from_version": "new-v1",
                    "to_version": "new-v2",
                }
            }
        )

    monkeypatch.setattr(execution, "observation", observation)
    await candidate.routing.setup(c)
    assert await c.routing_store.record_next()
    c.reflow_candidate_store = ReflowCandidateJobStore(c.db, clock=c.clock)
    c.reflow_candidate_ledger = ReflowCandidateLedger(c.db, clock=c.clock)
    c.app.state.reflow_candidate_queries = ReflowCandidateQueries(c.db, clock=c.clock)
    c.reflow_candidate_base = f"/api/v1/investigations/{c.started.run.id}/reinvestigation-candidate"
    model = candidate.Model()
    assert await candidate.worker(c, model).run_once()
    response = await candidate.get(c)
    assert response.status_code == 200 and response.json()["status"] == "succeeded", response.text
    assert len(model.calls) == 1
    c.app.state.reflow_plan_store = ReflowPlanStore(c.db, clock=c.clock)
    c.reflow_plan_base = c.reflow_candidate_base + "/action-plan"
    async with c.db() as session:
        version = await session.scalar(
            text("SELECT version FROM incidents WHERE id=:id"), {"id": c.started.run.incident_id}
        )
    result = response.json()["result"]
    c.reflow_plan_body = {
        "action": "create_plan",
        "candidate_result_sha256": raw_content_hash(result),
        "routing_sha256": result["routing_sha256"],
        "expected_incident_version": version,
        "idempotency_key": "reflow-plan-command-139",
        "reason": "工程师请求本次回流候选草案",
    }
    return response.json()


async def post(c, body=None, headers=None):
    return await c.client.post(
        c.reflow_plan_base,
        json=c.reflow_plan_body if body is None else body,
        headers=auth_header("engineer") if headers is None else headers,
    )


async def get(c, suffix="", headers=None):
    return await c.client.get(
        c.reflow_plan_base + suffix, headers=auth_header("viewer") if headers is None else headers
    )


async def plan_count(c):
    async with c.db() as session:
        return await session.scalar(text("SELECT count(*) FROM reinvestigation_candidate_plans"))


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_trusted_new_draft_full_binding_concurrent_idempotency_and_native_guards(
    wait_context,
    monkeypatch,
):
    c = wait_context
    before_candidate = await ready(c, monkeypatch)
    execution = candidate.routing.review.synthesis.execution
    before_root = await execution.dispatch.coordination.root_facts(c)
    before_calls = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    before_authorizations = await candidate.authorization_counts(c)
    assert (await get(c)).json() == {"record": None, "availability": "pending"}
    assert (await post(c, headers={})).status_code == 401
    for role in ("viewer", "approver", "executor", "alert_source"):
        assert (await post(c, headers=auth_header(role))).status_code == 403
    for suffix in ("", "/commands"):
        assert (await get(c, suffix, headers={})).status_code == 401
        assert (await get(c, suffix, headers=auth_header("alert_source"))).status_code == 403
    for index, change in enumerate(
        (
            {"candidate_result_sha256": "0" * 64},
            {"routing_sha256": "0" * 64},
            {"expected_incident_version": c.reflow_plan_body["expected_incident_version"] + 1},
        )
    ):
        response = await post(
            c, c.reflow_plan_body | change | {"idempotency_key": f"invalid-reflow-plan-{index}"}
        )
        assert response.status_code == 409, response.text
    assert await plan_count(c) == 0

    # 新告警发生在草案写入之后，必须整笔回滚，不能留下草案或成功审计。
    injected = False

    def append_alert_after_flush(session, flush_context):
        nonlocal injected
        if not injected and any(isinstance(row, ReflowPlanRow) for row in session.new):
            session.execute(
                text(
                    "INSERT INTO incident_events(incident_id,event_type,actor_type,to_status,"
                    "reason,incident_version,payload,occurred_at) SELECT id,"
                    "'incident.alert_attached','system',status,'模拟规划提交前新告警',version,"
                    "'{}'::jsonb,clock_timestamp() FROM incidents WHERE id=:id"
                ),
                {"id": c.started.run.incident_id},
            )
            injected = True

    event.listen(Session, "after_flush", append_alert_after_flush)
    try:
        try:
            rejected = await post(c)
        except DBAPIError:
            pass
        else:
            assert rejected.status_code == 409, rejected.text
    finally:
        event.remove(Session, "after_flush", append_alert_after_flush)
    assert injected, "必须实际写入草案后追加事件，不能把前置失败当提交门禁"
    assert await plan_count(c) == 0

    responses = await asyncio.gather(post(c), post(c))
    assert sorted(response.status_code for response in responses) == [200, 201], [
        r.text for r in responses
    ]
    created = next(response for response in responses if response.status_code == 201)
    assert created.headers["cache-control"] == "no-store"
    record = created.json()["plan"]["record"]
    content = record["content"]
    assert content["candidate_result_sha256"] == raw_content_hash(before_candidate["result"])
    assert content["routing_sha256"] == c.reflow_plan_body["routing_sha256"]
    assert (
        content["hypothesis"]
        == json.loads(before_candidate["packet"]["model_input"]["messages"][1]["content"])[
            "source"
        ]["review_source"]["synthesis_result"]["proposal"]
    )
    assert content["plan"]["status"] == "draft" and len(content["plan"]["steps"]) == 1
    assert content["plan"]["steps"] == [before_candidate["result"]["evaluation"]["step"]]
    assert (
        content["preconditions"]
        == before_candidate["result"]["evaluation"]["proposal"]["preconditions"]
    )
    assert (
        content["candidate_unresolved_questions"]
        == before_candidate["result"]["evaluation"]["proposal"]["unresolved_questions"]
    )
    assert content["approval_eligible"] is content["execution_eligible"] is False
    view = await get(c)
    assert view.status_code == 200 and view.json() == created.json()["plan"]
    assert view.headers["cache-control"] == "no-store"
    assert (await post(c)).status_code == 200
    for change in ({"reason": "不同命令"}, {"idempotency_key": "another-reflow-plan-139"}):
        assert (await post(c, c.reflow_plan_body | change)).status_code == 409
    audits = await get(c, "/commands")
    assert audits.status_code == 200 and audits.headers["cache-control"] == "no-store"
    outcomes = [item["outcome"] for item in audits.json()["items"]]
    assert outcomes.count("applied") == 1
    assert outcomes.count("replayed") >= 2 and outcomes.count("error") >= 5
    last_cursor = audits.json()["items"][-1]["cursor"]
    assert (await get(c, f"/commands?after_cursor={last_cursor}")).json()["items"] == []
    assert await plan_count(c) == 1
    assert await CallBudgetQueries(c.db).get_snapshot(c.started.run.id) == before_calls
    assert before_root == await execution.dispatch.coordination.root_facts(c)
    assert before_authorizations == await candidate.authorization_counts(c)
    assert before_candidate == (await candidate.get(c)).json()
    old_approval = await c.client.post(
        f"/api/v1/action-plans/{record['id']}/approval-requests",
        json={
            "expected_incident_version": c.reflow_plan_body["expected_incident_version"],
            "reason": "拒绝借用旧审批",
        },
        headers=auth_header("engineer"),
    )
    assert old_approval.status_code == 404, old_approval.text
    async with c.db() as session:
        native = await session.scalar(
            text("SELECT reflow_plan_content(:id,:at)"),
            {"id": c.started.run.id, "at": record["created_at"]},
        )
        assert native == content
    for sql in (
        "UPDATE reinvestigation_candidate_plans SET content_sha256=repeat('0',64)",
        "DELETE FROM reinvestigation_candidate_plans",
        "UPDATE reinvestigation_candidate_plan_commands SET command=command",
        "DELETE FROM reinvestigation_candidate_plan_commands",
    ):
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text(sql))
    # 自洽摘要也不能把草案的风险或执行资格提升为授权。
    for field, value in (("execution_eligible", True), ("approval_eligible", True)):
        forged = content | {field: value}
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text("SET LOCAL session_replication_role='replica'"))
                await session.execute(
                    text(
                        "UPDATE reinvestigation_candidate_plans SET content=CAST(:c AS jsonb),"
                        "content_sha256=reflow_hash(CAST(:c AS jsonb)) WHERE id=:id"
                    ),
                    {"c": json.dumps(forged), "id": UUID(record["id"])},
                )
                await session.execute(text("SET LOCAL session_replication_role='origin'"))
                await session.execute(
                    text(
                        "SELECT assert_reflow_plan(p,false) FROM reinvestigation_candidate_plans p"
                    )
                )
    with pytest.raises(DBAPIError):
        await asyncio.to_thread(command.downgrade, config(), "0093_reflow_candidate")
    await execution.dispatch.coordination.admission.attach_old_alert(c)
    historical = await get(c)
    assert historical.status_code == 200 and historical.json() == {
        "record": record,
        "availability": "stale",
    }
    assert (await post(c)).status_code == 200
    assert (
        await post(c, c.reflow_plan_body | {"idempotency_key": "stale-new-reflow-plan"})
    ).status_code == 409
    assert (await get(c, "/commands")).status_code == 200
