"""真实交接、PostgreSQL、可信 HTTP 命令及取消后的历史追溯。"""

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx2
import pytest
import pytest_asyncio
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

import opsswarm.investigation.handoff.persistence as handoff_module
import opsswarm.investigation.human_wait.store as wait_module
from opsswarm.checkpoints import initialize_checkpoint_schema, open_checkpoint_saver
from opsswarm.control_plane.app import create_app
from opsswarm.database import DatabaseSettings
from opsswarm.identity.models import OperatorAccessError, OperatorPrincipal, OperatorRole
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.investigation.attempts import ModelUsageTelemetry
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.errors import WorkItemLeaseLost
from opsswarm.investigation.handoff.persistence import TerminalHandoffStore
from opsswarm.investigation.human_wait.authorization import parse_command
from opsswarm.investigation.human_wait.store import HumanWaitStore
from opsswarm.investigation.human_wait.tables import HumanWaitCommandRow, HumanWaitEventRow
from opsswarm.investigation.models import EvidenceKind, WorkItemError
from opsswarm.investigation.persistence.tables import WorkItemRow
from opsswarm.investigation.tasks.queries import CollaborationQueries
from tests.identity_helpers import auth_header, auth_settings, token
from tests.integration.test_dynamic_first_round_graph import processor_context
from tests.integration.test_dynamic_review_graph import ReviewReplies
from tests.integration.test_dynamic_worker_handoff import claim, make_processor, setup_run
from tests.task_facts_helpers import BASE_TIME, TaskClock

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


@pytest_asyncio.fixture
async def wait_context(
    database_session_factory, migrated_database_url, checkpoint_database_url, request
):
    db = database_session_factory
    clock = TaskClock()
    settings = auth_settings()
    credentials = tuple(
        item.model_copy(update={"expires_at": BASE_TIME + timedelta(days=1)})
        for item in settings.credentials
    )
    engineer = next(item for item in credentials if item.credential_id == "engineer")
    settings = settings.model_copy(
        update={
            "credentials": (
                *credentials,
                engineer.model_copy(
                    update={
                        "credential_id": "engineer-two",
                        "subject_id": "trusted-second-engineer",
                    }
                ),
            )
        }
    )
    await initialize_checkpoint_schema(checkpoint_database_url)
    store, started = await setup_run(db)
    claimed = await claim(store, clock)
    replies = ReviewReplies(db, (EvidenceKind.LOG,), route_case="human")
    if getattr(request, "param", None) == "three_round":
        from tests.integration.test_dynamic_third_round_routing_graph import (
            ThirdRoundRoutingReplies,
        )

        replies = ThirdRoundRoutingReplies(db, third_route_case="round_limit")
    async with (
        httpx2.AsyncClient(transport=httpx2.MockTransport(replies)) as model_client,
        open_checkpoint_saver(checkpoint_database_url) as saver,
    ):
        outcome = await make_processor(db, model_client, saver, clock).process(
            processor_context(claimed)
        )
    handoffs = TerminalHandoffStore(db)
    handoff = await handoffs.handoff(
        claimed.work_item.id,
        lease_token=claimed.work_item.lease_token,
        source_review_route_id=outcome.source_review_route_id,
        source_sha256=outcome.source_sha256,
        model_name=outcome.model_name,
        usage=ModelUsageTelemetry(),
        clock=clock,
    )
    app = create_app(
        database_settings=DatabaseSettings(database_url=migrated_database_url),
        operator_auth_settings=settings,
        clock=clock,
    )
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://test"
        ) as client,
    ):
        app.state.human_wait_store = HumanWaitStore(db, clock=clock)
        yield SimpleNamespace(
            db=db,
            app=app,
            client=client,
            clock=clock,
            settings=settings,
            store=store,
            started=started,
            claimed=claimed,
            handoff=handoff,
            outcome=outcome,
            base=f"/api/v1/investigations/{started.run.id}/human-wait",
        )


def command(context, *, action="supplement", version=1, key="human-command-001", **changes):
    body = dict(
        action=action,
        handoff_id=str(context.handoff.id),
        source_sha256=context.handoff.source_sha256,
        expected_wait_version=version,
        idempotency_key=key,
        reason="工程师核对隔离演练调查",
    )
    if action == "supplement":
        body["items"] = [
            {"kind": "statement", "text": "部署记录中的版本号仍需人工核对。"},
            {"kind": "constraint", "text": "本轮只读，不允许执行回滚。"},
        ]
    return {**body, **changes}


async def post(context, body, *, header=None, run_id=None):
    base = context.base if run_id is None else f"/api/v1/investigations/{run_id}/human-wait"
    suffix = "supplements" if body.get("action", "supplement") == "supplement" else "cancel"
    return await context.client.post(
        f"{base}/{suffix}", json=body, headers=header or auth_header("engineer")
    )


async def state(context):
    response = await context.client.get(context.base, headers=auth_header("viewer"))
    assert response.status_code == 200, response.text
    return response.json()


async def count(context, table):
    async with context.db() as session:
        return await session.scalar(select(func.count()).select_from(table))


async def test_trusted_supplement_cancel_replays_and_preserves_business_history(wait_context):
    c = wait_context
    before_budget = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    before_work = await c.store.get_work_item(c.started.work_item.id)
    before_attempts = await c.store.list_attempts(c.started.run.id)
    assert (await state(c))["state_version"] == 1
    original = command(c)
    first = await post(c, original)
    assert first.status_code == 201, first.text
    event = first.json()["event"]
    assert event["subject_id"] == "trusted-engineer" and event["state_version"] == 2
    # 原始正文允许不同 JSON 表示，规范命令相同才可重放。
    alternate = {key: value for key, value in original.items() if key != "action"}
    alternate["reason"] = f"  {alternate['reason']}  "
    replay = await c.client.post(
        f"{c.base}/supplements",
        content=json.dumps(alternate, indent=2),
        headers={**auth_header("engineer"), "Content-Type": "application/json"},
    )
    assert replay.status_code == 200, replay.text
    assert replay.json() == {"applied": False, "event": event}
    second = await post(c, command(c, version=2, key="human-command-002"))
    assert second.status_code == 201, second.text
    cancellation_body = command(c, action="cancel", version=3, key="cancel-command-001")
    cancelled = await post(c, cancellation_body)
    assert cancelled.status_code == 201, cancelled.text
    assert (await post(c, cancellation_body)).status_code == 200
    assert (await post(c, original)).json() == {"applied": False, "event": event}
    final = await state(c)
    assert final["status"] == "cancelled" and final["state_version"] == 4
    assert len(final["events"]) == 3
    assert (await post(c, command(c, version=4, key="late-new-command"))).status_code == 409
    run = await c.store.get_run(c.started.run.id)
    assert run.status == "cancelled" and run.completed_at == c.clock()
    assert run.result is run.metrics is run.last_error is None
    assert await c.store.get_work_item(c.started.work_item.id) == before_work
    assert await c.store.list_attempts(c.started.run.id) == before_attempts
    assert await CallBudgetQueries(c.db).get_snapshot(run.id) == before_budget
    historic = await c.client.get(
        f"/api/v1/investigations/{run.id}/terminal-handoff", headers=auth_header("viewer")
    )
    assert historic.status_code == 200 and historic.json() == c.handoff.model_dump(mode="json")
    assert (
        await TerminalHandoffStore(c.db).handoff(
            c.claimed.work_item.id,
            lease_token=c.claimed.work_item.lease_token,
            source_review_route_id=c.outcome.source_review_route_id,
            source_sha256=c.outcome.source_sha256,
            model_name=c.outcome.model_name,
            usage=ModelUsageTelemetry(),
            clock=c.clock,
        )
        == c.handoff
    )
    assert (
        await c.store.claim_next(
            worker_id="late-worker", occurred_at=c.clock(), lease_duration=timedelta(seconds=30)
        )
        is None
    )
    with pytest.raises(WorkItemLeaseLost):
        await c.store.fail(
            c.claimed.work_item.id,
            lease_token=c.claimed.work_item.lease_token,
            occurred_at=c.clock(),
            error=WorkItemError(error_code="late", message="迟到结果"),
            retry_delay=timedelta(0),
        )
    incident = await c.client.get(
        f"/api/v1/incidents/{run.incident_id}", headers=auth_header("viewer")
    )
    assert incident.json()["status"] == "investigating"
    assert (
        await c.client.post(
            f"/api/v1/investigations/{run.id}/action-plans", headers=auth_header("engineer")
        )
    ).status_code == 409
    events = await CollaborationQueries(c.db).list_events(run.id)
    assert [item.event_type for item in events][-3:] == [
        "run.human_input_saved",
        "run.human_input_saved",
        "run.cancelled",
    ]
    after_cursor = events[-4].cursor
    assert len(await CollaborationQueries(c.db).list_events(run.id, after_cursor=after_cursor)) == 3
    page = await c.client.get(f"{c.base}/commands?limit=2", headers=auth_header("viewer"))
    assert page.status_code == 200 and len(page.json()["items"]) == 2
    assert page.json()["next_cursor"] is not None
    assert "raw_body" not in page.text
    seen = page.json()["items"]
    cursor = page.json()["next_cursor"]
    rest = await c.client.get(
        f"{c.base}/commands?after_cursor={cursor}", headers=auth_header("viewer")
    )
    seen += rest.json()["items"]
    assert [item["outcome"] for item in seen] == [
        "applied",
        "replayed",
        "applied",
        "applied",
        "replayed",
        "replayed",
        "error",
    ]


async def test_wait_commands_require_trusted_identity_and_strict_payload(wait_context):
    c = wait_context
    for path in [c.base, f"{c.base}/commands"]:
        assert (await c.client.get(path)).status_code == 401
        assert (await c.client.get(path, headers=auth_header("alert_source"))).status_code == 403
    for action in ["supplement", "cancel"]:
        body = command(c, action=action)
        suffix = "supplements" if action == "supplement" else "cancel"
        assert (await c.client.post(f"{c.base}/{suffix}", json=body)).status_code == 401
        for role in ["viewer", "approver", "executor", "alert_source"]:
            assert (await post(c, body, header=auth_header(role))).status_code == 403
    for change in [
        {"actor_id": "forged"},
        {"role": "engineer"},
        {"authorization_id": str(uuid4())},
        {"expected_wait_version": True},
        {"items": []},
        {"reason": "   "},
        {"items": [{"kind": "evidence", "text": "伪装工具证据"}]},
    ]:
        assert (await post(c, command(c, **change))).status_code == 422
    large = json.dumps(command(c)) + " " * 65536
    too_large = await c.client.post(
        f"{c.base}/supplements",
        content=large,
        headers={**auth_header("engineer"), "Content-Type": "application/json"},
    )
    assert too_large.status_code == 413
    legacy = create_app(
        operator_auth_settings=OperatorAuthSettings(mode="legacy_local"), clock=c.clock
    )
    legacy.state.human_wait_store = c.app.state.human_wait_store
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=legacy), base_url="http://test"
    ) as client:
        assert (await client.get(c.base)).status_code == 401
        assert (
            await client.post(f"{c.base}/cancel", json=command(c, action="cancel"))
        ).status_code == 401
    assert await count(c, HumanWaitEventRow) == await count(c, HumanWaitCommandRow) == 0


async def test_wait_business_refusals_are_audited_and_subject_keys_are_isolated(wait_context):
    c = wait_context
    valid = command(c)
    unknown = await post(c, valid, run_id=uuid4())
    assert unknown.status_code == 404
    _, pending = await setup_run(c.db)
    assert (await post(c, valid, run_id=pending.run.id)).status_code == 409
    for changes, code in [
        ({"handoff_id": str(uuid4())}, "human_wait_source_conflict"),
        ({"source_sha256": "a" * 64}, "human_wait_source_conflict"),
        ({"expected_wait_version": 2}, "human_wait_version_conflict"),
        (
            {
                "items": [
                    {
                        "kind": "statement",
                        "text": "未来观察",
                        "observed_at": (c.clock() + timedelta(seconds=1)).isoformat(),
                    }
                ]
            },
            "human_wait_future_observation",
        ),
    ]:
        response = await post(c, {**valid, **changes})
        assert response.status_code == 409 and response.json()["error_code"] == code, response.text
    first = await post(c, valid)
    assert first.status_code == 201, first.text
    assert (await post(c, {**valid, "reason": "同键不同内容"})).json()[
        "error_code"
    ] == "human_wait_idempotency_conflict"
    second_header = {"Authorization": f"Bearer engineer-two.{token('engineer').split('.')[1]}"}
    stale = await post(c, valid, header=second_header)
    assert stale.status_code == 409
    new_subject = await post(c, {**valid, "expected_wait_version": 2}, header=second_header)
    assert new_subject.status_code == 201
    assert new_subject.json()["event"]["subject_id"] == "trusted-second-engineer"
    assert await count(c, HumanWaitEventRow) == 2
    async with c.db() as session:
        failures = list(
            await session.scalars(
                select(HumanWaitCommandRow).where(HumanWaitCommandRow.outcome == "error")
            )
        )
        assert len(failures) == 8
        assert all(item.event_id is None for item in failures)


@pytest.mark.parametrize("second_action", ["supplement", "cancel"])
async def test_wait_concurrent_version_competition_has_one_winner(wait_context, second_action):
    c = wait_context
    responses = await asyncio.gather(
        post(c, command(c, key="concurrent-input-one")),
        post(c, command(c, action=second_action, key="concurrent-input-two")),
    )
    assert sorted(response.status_code for response in responses) == [201, 409]
    current = await state(c)
    assert len(current["events"]) == 1 and current["state_version"] == 2
    assert await count(c, HumanWaitCommandRow) == 2
    if current["status"] == "waiting":
        final = await post(c, command(c, action="cancel", version=2, key="final-cancel-one"))
        assert final.status_code == 201, final.text
    assert (await state(c))["status"] == "cancelled"


async def test_wait_bound_keeps_cancel_capacity_and_old_replay(wait_context):
    c = wait_context
    first = command(c, key="bounded-command-01")
    for index in range(15):
        response = await post(
            c, command(c, version=index + 1, key=f"bounded-command-{index + 1:02}")
        )
        assert response.status_code == 201, response.text
    full = await post(c, command(c, version=16, key="too-many-inputs-01"))
    assert full.status_code == 409 and full.json()["error_code"] == "human_wait_input_limit"
    cancelled = await post(c, command(c, action="cancel", version=16, key="bounded-cancel-01"))
    assert cancelled.status_code == 201, cancelled.text
    assert (await state(c))["state_version"] == 17
    assert (await post(c, first)).status_code == 200
    assert await count(c, HumanWaitEventRow) == 16


async def test_wait_rejects_source_tampering_and_native_fact_mutation(wait_context):
    c = wait_context
    accepted = await post(c, command(c))
    assert accepted.status_code == 201, accepted.text
    auth_id = accepted.json()["event"]["authorization_id"]
    for table in ["investigation_human_wait_events", "investigation_human_wait_commands"]:
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with c.db() as session, session.begin():
                await session.execute(text(f"DELETE FROM {table}"))
    async with c.db() as session, session.begin():
        await session.execute(
            text("UPDATE operator_authorizations SET body_sha256=:hash WHERE id=:id"),
            {"id": auth_id, "hash": "a" * 64},
        )
    for path in [
        c.base,
        f"{c.base}/commands",
        f"/api/v1/investigations/{c.started.run.id}/terminal-handoff",
    ]:
        result = await c.client.get(path, headers=auth_header("viewer"))
        assert (
            result.status_code == 409
            and result.json()["error_code"] == "human_wait_integrity_violation"
        )


async def test_wait_authentication_expiry_during_lock_wait_blocks_write(wait_context, monkeypatch):
    c = wait_context
    entered = asyncio.Event()
    original = wait_module._locked_run

    async def entered_lock(*args, **kwargs):
        entered.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(wait_module, "_locked_run", entered_lock)
    async with c.db() as blocker, blocker.begin():
        await blocker.scalar(
            select(WorkItemRow).where(WorkItemRow.id == c.started.work_item.id).with_for_update()
        )
        request = asyncio.create_task(post(c, command(c)))
        await asyncio.wait_for(entered.wait(), 10)
        c.clock.current = BASE_TIME + timedelta(days=1)
    result = await request
    assert result.status_code == 401, result.text
    c.clock.current = BASE_TIME + timedelta(seconds=4)
    assert await count(c, HumanWaitEventRow) == await count(c, HumanWaitCommandRow) == 0
    assert (await state(c))["state_version"] == 1


async def test_wait_expiry_after_writes_rolls_back_cancel_and_audit(wait_context, monkeypatch):
    c = wait_context
    original = wait_module.append_task_event

    async def expire_after_autoflush(*args, **kwargs):
        await original(*args, **kwargs)
        c.clock.current = BASE_TIME + timedelta(days=1)

    monkeypatch.setattr(wait_module, "append_task_event", expire_after_autoflush)
    response = await post(c, command(c, action="cancel"))
    assert response.status_code == 401, response.text
    c.clock.current = BASE_TIME + timedelta(seconds=4)
    assert await count(c, HumanWaitEventRow) == await count(c, HumanWaitCommandRow) == 0
    assert (await c.store.get_run(c.started.run.id)).status == "await_human"
    assert (await state(c))["state_version"] == 1
    events = await CollaborationQueries(c.db).list_events(c.started.run.id)
    assert events[-1].event_type == "run.handed_off"


async def test_wait_same_admission_retry_checks_expiry_after_authorization_lock(
    wait_context, monkeypatch
):
    c = wait_context
    unknown_id = uuid4()
    body = command(c)
    refused = await post(c, body, run_id=unknown_id)
    assert refused.status_code == 404
    auth_id = UUID(refused.headers["X-Authorization-ID"])
    principal = OperatorPrincipal(
        subject_id="trusted-engineer",
        credential_id="engineer",
        role=OperatorRole.ENGINEER,
        expires_at=BASE_TIME + timedelta(days=1),
    )
    entered = asyncio.Event()
    original = AsyncSession.get

    async def entered_authorization_lock(self, entity, ident, **kwargs):
        if entity is OperatorAuthorizationRow and kwargs.get("with_for_update"):
            entered.set()
        return await original(self, entity, ident, **kwargs)

    async with c.db() as blocker, blocker.begin():
        await blocker.get(OperatorAuthorizationRow, auth_id, with_for_update=True)
        monkeypatch.setattr(AsyncSession, "get", entered_authorization_lock)
        retry = asyncio.create_task(
            c.app.state.human_wait_store.submit(
                unknown_id,
                parse_command(body),
                principal=principal,
                authorization_id=auth_id,
                raw_body=refused.request.content,
            )
        )
        await asyncio.wait_for(entered.wait(), 10)
        c.clock.current = principal.expires_at
    with pytest.raises(OperatorAccessError, match="认证已经过期"):
        await retry
    assert await count(c, HumanWaitCommandRow) == 1
    assert await count(c, HumanWaitEventRow) == 0


async def test_historical_handoff_read_serializes_with_cancel(wait_context, monkeypatch):
    c = wait_context
    entered, release = asyncio.Event(), asyncio.Event()
    original = handoff_module.validated_handoff
    first_read = True

    async def paused_read(*args, **kwargs):
        nonlocal first_read
        if first_read:
            first_read = False
            entered.set()
            await asyncio.wait_for(release.wait(), 10)
        return await original(*args, **kwargs)

    monkeypatch.setattr(handoff_module, "validated_handoff", paused_read)
    historic = asyncio.create_task(TerminalHandoffStore(c.db).get(c.started.run.id))
    await asyncio.wait_for(entered.wait(), 10)
    # 用 PostgreSQL NOWAIT 确认查询确实持有共享锁，避免仅凭协程尚未结束判断阻塞。
    with pytest.raises(DBAPIError) as locked_error:
        async with c.db() as probe, probe.begin():
            await probe.scalar(
                select(WorkItemRow)
                .where(WorkItemRow.id == c.started.work_item.id)
                .with_for_update(nowait=True)
            )
    assert locked_error.value.orig.sqlstate == "55P03"
    reached_lock = asyncio.Event()
    locked = wait_module._locked_run

    async def entered_work_lock(*args, **kwargs):
        reached_lock.set()
        return await locked(*args, **kwargs)

    monkeypatch.setattr(wait_module, "_locked_run", entered_work_lock)
    cancellation = asyncio.create_task(post(c, command(c, action="cancel")))
    await asyncio.wait_for(reached_lock.wait(), 10)
    assert not cancellation.done()
    release.set()
    assert await historic == c.handoff
    response = await cancellation
    assert response.status_code == 201, response.text
    assert await TerminalHandoffStore(c.db).get(c.started.run.id) == c.handoff
