"""复用一份140历史，验收真实拒绝分支、可信批准及原生事务守卫。"""

import asyncio
import copy
import json
from datetime import datetime, timedelta
from uuid import UUID

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import Session

from alembic import command
from opsswarm.approval.reinvestigation_decision.store import ReflowApprovalDecisionStore
from opsswarm.approval.reinvestigation_decision.tables import ReflowApprovalDecisionRow
from opsswarm.control_plane.sse import IncidentEventStream
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.verification.candidate.contracts import ProbeObservation
from opsswarm.verification.candidate.tables import RecoverySampleRow
from tests import conftest
from tests.api import test_candidate_settlement_api as settlement
from tests.api import test_reinvestigation_approval_api as approval
from tests.api import test_reinvestigation_coordination_api as coordination
from tests.api import test_release_execution_jobs_api as release
from tests.api.test_candidate_approval_api import incident_events
from tests.api.test_reinvestigation_approval_api import current_fixture_time as current_fixture_time
from tests.api.test_reinvestigation_approval_api import (
    initial_reflow_capacity as initial_reflow_capacity,
)
from tests.api.test_reinvestigation_approval_api import wait_context as wait_context
from tests.identity_helpers import auth_header
from tests.integration import test_dynamic_worker_handoff as worker_fixture
from tests.integration.test_candidate_approval_migration import config

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest.fixture(autouse=True)
def original_run_duration(monkeypatch):
    """长链从创建时固定为既有合法上限，后续仍逐阶段校验同一截止。"""
    original = worker_fixture.CallBudgetPolicy

    def policy(**kwargs):
        return original(**(kwargs | {"max_duration_seconds": 1800}))

    monkeypatch.setattr(worker_fixture, "CallBudgetPolicy", policy)


@pytest.fixture(autouse=True)
def recovery_prefix(monkeypatch):
    """固定样本由原生守卫验收，结果仍由应用服务判定，不重复旧Worker调度。"""
    original = settlement.recover

    async def recover(c, mode="healthy"):
        if mode != "not_recovered":
            return await original(c, mode)
        claim = await c.recovery.claim_next(worker_id="reflow-decision-prefix")
        assert claim is not None
        samples = []
        for sequence in range(1, 4):
            if samples:
                while c.clock() < samples[-1] + timedelta(seconds=2):
                    await asyncio.sleep(0.1)
            async with c.db() as session, session.begin():
                at = c.clock()
                observation = ProbeObservation(
                    requested_at=at, observed_at=at, http_status=500, value=1.0, order_id=None
                )
                session.add(
                    RecoverySampleRow(
                        attempt_id=claim.attempt_id,
                        job_id=claim.job_id,
                        sequence=sequence,
                        observation=observation.model_dump(mode="json"),
                        error_code=None,
                        created_at=at,
                    )
                )
                await session.flush()
            samples.append(at)
        await c.recovery.complete(claim)
        response = await settlement.recovery_get(c)
        assert response.status_code == 200, response.text
        assert response.json()["result"]["reason_code"] == "business_probe_failed"
        return response.json(), samples

    monkeypatch.setattr(settlement, "recover", recover)


@pytest.fixture(autouse=True)
def closed_change_window(monkeypatch):
    """change窗口按秒向内取整，真实留出完整秒，不依赖查询耗时。"""
    original = coordination.prepare

    async def prepare(c, *args, **kwargs):
        await original(c, *args, **kwargs)
        await asyncio.sleep(2)

    monkeypatch.setattr(coordination, "prepare", prepare)


@pytest.fixture(scope="session")
def memory_postgres_data():
    """完整历史仍走真实PG；临时内存盘减少短租约前置链的磁盘开销。"""
    original = conftest.PostgresContainer

    def container(*args, **kwargs):
        return (
            original(*args, **kwargs)
            .with_name("opsswarm-141-postgres")
            .with_tmpfs_mount("/var/lib/postgresql/data", size="size=512m")
            .with_command(["postgres", "-c", "jit=off"])
        )

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(conftest, "PostgresContainer", container)
        yield


@pytest.fixture(scope="session")
def migrated_database_url(memory_postgres_data, migrated_database_url):
    # 显式先挂载内存盘，再进入上层迁移夹具，避免session夹具的隐式顺序。
    return migrated_database_url


@pytest.fixture(autouse=True)
def live_preparation_clock(monkeypatch):
    """准备观察从实际采集时刻计时，不依赖旧调查夹具的冻结时间。"""
    original = release.preparation.post

    async def post(c, *args, **kwargs):
        c.clock.live = True
        return await original(c, *args, **kwargs)

    monkeypatch.setattr(release.preparation, "post", post)


async def ready(c, monkeypatch):
    await approval.ready(c, monkeypatch)
    created = await approval.post(c)
    assert created.status_code == 201, created.text
    c.reflow_request_record = created.json()["request"]["record"]
    c.reflow_decision_base = c.reflow_approval_base + "/decision"
    c.app.state.reflow_approval_decision_store = ReflowApprovalDecisionStore(c.db, clock=c.clock)
    c.reflow_decision_body = {
        "action": "approve",
        "request_id": c.reflow_request_record["id"],
        "plan_content_sha256": c.reflow_request_record["plan_content_sha256"],
        "expected_incident_version": c.reflow_request_record["incident_version"],
        "acknowledge_unresolved_conditions": True,
        "idempotency_key": "reflow-approval-decision-141",
        "reason": "保留本次完整新草案及未决条件，记录可信人工批准",
    }
    # 同一可信主体可以在不同授权角色下工作，不额外引入四眼主体互斥规则。
    settings = c.app.state.operator_auth_settings
    c.app.state.operator_auth_settings = settings.model_copy(
        update={
            "credentials": tuple(
                credential.model_copy(
                    update={"subject_id": c.reflow_request_record["requested_by_id"]}
                )
                if credential.role == "approver"
                else credential
                for credential in settings.credentials
            )
        }
    )


async def post(c, body=None, headers=None):
    return await c.client.post(
        c.reflow_decision_base,
        json=c.reflow_decision_body if body is None else body,
        headers=auth_header("approver") if headers is None else headers,
    )


async def get(c, suffix="", headers=None):
    return await c.client.get(
        c.reflow_decision_base + suffix,
        headers=auth_header("viewer") if headers is None else headers,
    )


async def decision_count(c):
    async with c.db() as session:
        return await session.scalar(text("SELECT count(*) FROM reinvestigation_approval_decisions"))


async def reject_branch(c):
    """分支使用真实应用服务与原生守卫，外层回滚后复用完整140历史。"""
    original_store = c.app.state.reflow_approval_decision_store
    engine = c.db.kw["bind"]
    async with engine.connect() as connection:
        transaction = await connection.begin()
        try:
            factory = async_sessionmaker(
                bind=connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
            )
            c.app.state.reflow_approval_decision_store = ReflowApprovalDecisionStore(
                factory, clock=c.clock
            )
            await connection.execute(
                text(
                    "INSERT INTO incident_events(incident_id,event_type,actor_type,to_status,"
                    "reason,incident_version,payload,occurred_at) SELECT id,"
                    "'incident.alert_attached','system',status,'模拟拒绝时依据已过时',version,"
                    "'{}'::jsonb,clock_timestamp() FROM incidents WHERE id=:id"
                ),
                {"id": c.started.run.incident_id},
            )
            response = await post(
                c,
                c.reflow_decision_body
                | {
                    "action": "reject",
                    "acknowledge_unresolved_conditions": False,
                    "idempotency_key": "reject-reflow-branch-141",
                    "reason": "依据已变，拒绝此次候选",
                },
            )
            assert response.status_code == 201, response.text
            view = response.json()["decision"]
            assert view["availability"] == "recorded" and view["record"]["outcome"] == "rejected"
            assert (await get(c)).json() == view
            state = (
                await connection.execute(
                    text("SELECT status,version FROM incidents WHERE id=:id"),
                    {"id": c.started.run.incident_id},
                )
            ).one()
            assert state.status == "investigating"
            assert state.version == c.reflow_request_record["incident_version"] + 1
            await connection.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            # 同一真实来源下，未到期的expire即使拥有自洽内容/迁移也必须被SQL拒绝。
            record = view["record"]
            premature = copy.deepcopy(record["content"])
            premature["outcome"] = "expired"
            wrote_forged = False
            with pytest.raises(DBAPIError, match="动作必须匹配原请求到期时刻"):
                async with connection.begin_nested():
                    await connection.execute(text("SET LOCAL session_replication_role='replica'"))
                    await connection.execute(
                        text(
                            "UPDATE incident_events SET payload='{"
                            + '"action":"expire_approval"'
                            + "}'::jsonb WHERE id=:id"
                        ),
                        {"id": record["event_cursor"]},
                    )
                    await connection.execute(
                        text(
                            "UPDATE reinvestigation_approval_decisions SET outcome='expired',"
                            "content=CAST(:c AS jsonb),"
                            "content_sha256=reflow_hash(CAST(:c AS jsonb)) WHERE id=:id"
                        ),
                        {"c": json.dumps(premature), "id": UUID(record["id"])},
                    )
                    wrote_forged = True
                    await connection.execute(text("SET LOCAL session_replication_role='origin'"))
                    await connection.execute(
                        text(
                            "SELECT assert_reflow_approval_decision(d,false) "
                            "FROM reinvestigation_approval_decisions d"
                        )
                    )
            assert wrote_forged, "先通过基础形状，再由原请求真实时限拒绝expire"
            assert (await get(c)).json() == view
        finally:
            await transaction.rollback()
            c.app.state.reflow_approval_decision_store = original_store


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_trusted_decision_reject_branch_and_approved_phase_without_execution(
    wait_context,
    monkeypatch,
):
    c = wait_context
    await ready(c, monkeypatch)
    execution = approval.plan.candidate.routing.review.synthesis.execution
    old_root = await execution.dispatch.coordination.root_facts(c)
    old_budget = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    old_authorizations = await approval.plan.candidate.authorization_counts(c)
    old_request = (await approval.get(c)).json()
    old_events = await incident_events(c)
    before_incident = await approval.incident_fact(c)
    assert before_incident.status == "awaiting_approval"
    assert (await get(c)).json() == {"record": None, "availability": "pending"}
    assert (await post(c, headers={})).status_code == 401
    for role in ("viewer", "engineer", "executor", "alert_source"):
        assert (await post(c, headers=auth_header(role))).status_code == 403
    for suffix in ("", "/commands"):
        assert (await get(c, suffix, headers={})).status_code == 401
        assert (await get(c, suffix, headers=auth_header("alert_source"))).status_code == 403
    for change in (
        {"acknowledge_unresolved_conditions": 1},
        {"acknowledge_unresolved_conditions": False},
        {"execution_eligible": True},
    ):
        assert (await post(c, c.reflow_decision_body | change)).status_code == 422
    for index, change in enumerate(
        (
            {"plan_content_sha256": "0" * 64},
            {"expected_incident_version": before_incident.version + 1},
            {"action": "expire", "acknowledge_unresolved_conditions": False},
        )
    ):
        response = await post(
            c,
            c.reflow_decision_body
            | change
            | {"idempotency_key": f"invalid-reflow-decision-{index}"},
        )
        assert response.status_code == 409, response.text
    await reject_branch(c)
    assert await decision_count(c) == 0
    assert await approval.incident_fact(c) == before_incident
    assert old_request == (await approval.get(c)).json()

    for status in ("verifying", "resolved"):
        with pytest.raises(DBAPIError, match="真实待审批阶段及相邻版本"):
            async with c.db() as session, session.begin():
                await session.execute(
                    text("UPDATE incidents SET status=:status,version=version+1 WHERE id=:id"),
                    {"status": status, "id": c.started.run.incident_id},
                )
    for status in ("mitigating", "investigating"):
        wrote_transition = False
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(
                    text("UPDATE incidents SET status=:status,version=version+1 WHERE id=:id"),
                    {"status": status, "id": c.started.run.incident_id},
                )
                wrote_transition = True
        assert wrote_transition, "孤立退出待审批应在COMMIT因缺少决定、事件和applied审计而拒绝"
    injected = False

    def attach_alert_after_decision(session, flush_context):
        nonlocal injected
        if not injected and any(isinstance(row, ReflowApprovalDecisionRow) for row in session.new):
            session.execute(
                text(
                    "INSERT INTO incident_events(incident_id,event_type,actor_type,to_status,"
                    "reason,incident_version,payload,occurred_at) SELECT id,"
                    "'incident.alert_attached','system',status,'模拟批准写后出现新告警',version,"
                    "'{}'::jsonb,clock_timestamp() FROM incidents WHERE id=:id"
                ),
                {"id": c.started.run.incident_id},
            )
            injected = True

    event.listen(Session, "after_flush", attach_alert_after_decision)
    try:
        try:
            refused = await post(c)
        except DBAPIError:
            if not injected:
                raise
        else:
            assert refused.status_code == 409, refused.text
            assert injected, refused.text
    finally:
        event.remove(Session, "after_flush", attach_alert_after_decision)
    assert injected, "必须实际插入决定后追加告警，避免前置失败冒充提交守卫"
    assert await decision_count(c) == 0
    assert await approval.incident_fact(c) == before_incident
    assert len(await incident_events(c)) == len(old_events)
    responses = await asyncio.gather(post(c), post(c))
    assert sorted(response.status_code for response in responses) == [200, 201], [
        r.text for r in responses
    ]
    created = next(response for response in responses if response.status_code == 201)
    assert created.headers["cache-control"] == "no-store"
    view = created.json()["decision"]
    record = view["record"]
    assert view["availability"] == "current" and record["outcome"] == "approved"
    assert record["request_id"] == c.reflow_request_record["id"]
    assert record["plan_content_sha256"] == c.reflow_plan_record["content_sha256"]
    assert record["basis_event_cursor"] == old_events[-1].id
    assert record["previous_incident_version"] == before_incident.version
    assert record["incident_version"] == before_incident.version + 1
    assert record["decided_by_id"] == c.reflow_request_record["requested_by_id"]
    assert record["content"]["plan"] == c.reflow_plan_record["content"]
    assert record["content"]["execution_eligible"] is False
    assert (await get(c)).json() == view
    assert (await get(c)).headers["cache-control"] == "no-store"
    assert (await post(c)).json() == {"applied": False, "decision": view}
    assert (await approval.get(c)).json() == {
        "record": c.reflow_request_record,
        "availability": "stale",
    }
    assert old_root == await execution.dispatch.coordination.root_facts(c)
    assert old_budget == await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    assert old_authorizations == await approval.plan.candidate.authorization_counts(c)
    events = await incident_events(c)
    assert len(events) == len(old_events) + 1
    mutation = events[-1]
    assert mutation.id == record["event_cursor"]
    assert (mutation.from_status, mutation.to_status) == ("awaiting_approval", "mitigating")
    assert mutation.actor_id == record["decided_by_id"]
    assert mutation.payload == {"action": "approve_plan"}
    assert mutation.occurred_at == datetime.fromisoformat(record["created_at"])
    assert mutation.incident_version == record["incident_version"]
    replay = await c.client.get(
        f"/api/v1/incidents/{c.started.run.incident_id}/events?after_cursor={old_events[-1].id}",
        headers=auth_header("viewer"),
    )
    assert replay.status_code == 200 and len(replay.json()["items"]) == 1

    async def connected():
        return False

    stream = IncidentEventStream(c.app.state.incident_store).stream(
        c.started.run.incident_id, after_cursor=old_events[-1].id, is_disconnected=connected
    )
    try:
        frame = await anext(stream)
        assert (
            f"id: {record['event_cursor']}\n" in frame
            and "event: incident.status_changed\n" in frame
        )
    finally:
        await stream.aclose()
    for change in ({"reason": "不同决定原因"}, {"idempotency_key": "different-reflow-decision"}):
        assert (await post(c, c.reflow_decision_body | change)).status_code == 409
    audits = await get(c, "/commands")
    assert audits.status_code == 200 and audits.headers["cache-control"] == "no-store"
    outcomes = [item["outcome"] for item in audits.json()["items"]]
    assert outcomes.count("applied") == 1 and outcomes.count("replayed") == 2
    assert outcomes.count("error") == 5
    assert "raw_body" not in audits.text
    for sql in (
        "UPDATE reinvestigation_approval_decisions SET content=content",
        "DELETE FROM reinvestigation_approval_decisions",
        "UPDATE reinvestigation_approval_decision_commands SET command=command",
        "DELETE FROM reinvestigation_approval_decision_commands",
    ):
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text(sql))
    for change in (
        {"execution_eligible": True},
        {"plan": record["content"]["plan"] | {"candidate_unresolved_questions": []}},
    ):
        forged = record["content"] | change
        wrote_forged = False
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text("SET LOCAL session_replication_role='replica'"))
                await session.execute(
                    text(
                        "UPDATE reinvestigation_approval_decisions SET content=CAST(:c AS jsonb),"
                        "content_sha256=reflow_hash(CAST(:c AS jsonb)) WHERE id=:id"
                    ),
                    {"c": json.dumps(forged), "id": UUID(record["id"])},
                )
                wrote_forged = True
                await session.execute(text("SET LOCAL session_replication_role='origin'"))
                await session.execute(
                    text(
                        "SELECT assert_reflow_approval_decision(d,false) "
                        "FROM reinvestigation_approval_decisions d"
                    )
                )
        assert wrote_forged
    with pytest.raises(DBAPIError):
        await asyncio.to_thread(command.downgrade, config(), "0095_reflow_approval_request")
    old_execution = await c.client.post(
        f"/api/v1/action-steps/{record['id']}/executions",
        json={
            "expected_incident_version": record["incident_version"],
            "idempotency_key": "reject-old-reflow-execution",
        },
        headers=auth_header("executor"),
    )
    assert old_execution.status_code == 404, old_execution.text
    await execution.dispatch.coordination.admission.attach_old_alert(c)
    assert (await get(c)).json() == {"record": record, "availability": "stale"}
    store = c.app.state.reflow_approval_decision_store
    old_clock = store._clock
    store._clock = lambda: old_budget.deadline + timedelta(seconds=1)
    try:
        assert (await get(c)).json() == {"record": record, "availability": "expired"}
    finally:
        store._clock = old_clock
    assert (await post(c)).status_code == 200
    assert (
        await post(c, c.reflow_decision_body | {"idempotency_key": "new-stale-reflow-decision"})
    ).status_code == 409
    assert await decision_count(c) == 1
