"""真实PostgreSQL验证远端窗口、实际CAS、取消竞争与原生防绕过。"""

import asyncio
import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import httpx2
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command as alembic_command
from opsswarm.fault_lab.app import create_app
from opsswarm.fault_lab.controller import FaultController
from opsswarm.fault_lab.release.contracts import ReleaseCommand, ReleaseError
from opsswarm.fault_lab.release.dispatch_contracts import DispatchCommand
from opsswarm.fault_lab.release.policy import command_hash
from opsswarm.fault_lab.release.store import LabReleaseStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
PATH = "/__admin/simulated-release/dispatches"
BAD = "2026.01.01-rc1"


def config():
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def make_app(factory):
    store = LabReleaseStore(factory)
    await store.initialize()
    await store.change(
        ReleaseCommand(
            expected_version="stable",
            expected_revision=1,
            target_version=BAD,
            idempotency_key="dispatch-fixture-release",
        )
    )
    app = create_app(FaultController(), lab_release_store=store)
    return app


async def body(factory, *, seconds=20):
    async with factory() as session:
        now = await session.scalar(text("SELECT clock_timestamp()"))
    operation = uuid4()
    return DispatchCommand(
        operation_id=operation,
        command=ReleaseCommand(
            expected_version=BAD,
            expected_revision=2,
            target_version="stable",
            idempotency_key=f"candidate-release-{operation}",
        ),
        not_before=now - timedelta(seconds=1),
        expires_at=now + timedelta(seconds=seconds),
    )


async def snapshot(factory):
    async with factory() as session:
        return (
            (
                await session.execute(
                    text(
                        "SELECT version,revision,command_count,last_command_id,updated_at "
                        "FROM lab_release_states"
                    )
                )
            ).all(),
            await session.scalar(text("SELECT count(*) FROM lab_release_commands")),
            (
                await session.execute(
                    text(
                        "SELECT operation_id,status,command_id,finished_at "
                        "FROM lab_release_dispatches"
                    )
                )
            ).all(),
        )


async def wait_past(session, deadline):
    await session.execute(
        text(
            "SELECT pg_sleep(GREATEST(0,EXTRACT(EPOCH FROM"
            "(CAST(:deadline AS timestamptz)-clock_timestamp())))+0.05)"
        ),
        {"deadline": deadline},
    )


async def native_insert(session, value, *, created_at=None):
    now = await session.scalar(text("SELECT clock_timestamp()"))
    values = dict(
        id=str(uuid4()),
        service_name="order-service",
        sequence=2,
        kind="cas",
        idempotency_key=value.command.idempotency_key,
        command=value.command.model_dump(mode="json"),
        command_sha256=command_hash(value.command.model_dump(mode="json")),
        previous_version=BAD,
        previous_revision=2,
        version="stable",
        revision=3,
        created_at=(now if created_at is None else created_at).isoformat(),
    )
    await session.execute(
        text(
            "INSERT INTO lab_release_commands SELECT * FROM "
            "jsonb_populate_record(NULL::lab_release_commands,CAST(:value AS jsonb))"
        ),
        {"value": json.dumps(values)},
    )


async def test_0081_metadata_empty_roundtrip_and_unavailable_endpoints(database_session_factory):
    await asyncio.to_thread(alembic_command.check, config())
    try:
        await asyncio.to_thread(
            alembic_command.downgrade, config(), "0080_candidate_release_request"
        )
        await asyncio.to_thread(alembic_command.upgrade, config(), "head")
        await asyncio.to_thread(alembic_command.check, config())
    finally:
        await asyncio.to_thread(alembic_command.upgrade, config(), "head")
    app = create_app(FaultController())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://lab"
    ) as client:
        assert (await client.get(PATH + "/" + str(uuid4()))).status_code == 503


async def test_register_execute_replay_restart_and_read_only_receipt(database_session_factory):
    factory = database_session_factory
    app = await make_app(factory)
    value = await body(factory)
    before = await snapshot(factory)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://lab"
    ) as client:
        path = PATH + "/" + str(value.operation_id)
        created = await client.post(PATH, json=value.model_dump(mode="json"))
        assert created.status_code == 201, created.text
        assert created.json()["dispatch"]["availability"] == "ready"
        assert (await snapshot(factory))[:2] == before[:2]
        missing = await client.get(PATH + "/" + str(uuid4()))
        assert missing.status_code == 404 and missing.headers["cache-control"] == "no-store"
        assert (
            await client.put(path, json={"command": value.command.model_dump(mode="json")})
        ).status_code == 422
        replay = await client.post(PATH, json=value.model_dump(mode="json"))
        assert replay.status_code == 200
        assert replay.json()["applied"] is False
        modified = value.model_dump(mode="json")
        modified["expires_at"] = (value.expires_at - timedelta(seconds=1)).isoformat()
        assert (await client.post(PATH, json=modified)).status_code == 409
        done = await client.put(path)
        assert done.status_code == 201, done.text
        result = done.json()["dispatch"]
        assert result["record"]["status"] == "applied"
        assert result["receipt"]["current"]["version"] == "stable"
        assert result["receipt"]["command"]["command"] == value.command.model_dump(mode="json")
        assert result["receipt"]["command"]["id"] == result["record"]["command_id"]
        assert done.headers["cache-control"] == "no-store"
        after = await snapshot(factory)
        assert (await client.put(path)).status_code == 200
        assert (await client.delete(path)).status_code == 409
        assert (await client.get(path)).json()["record"] == result["record"]
        assert await snapshot(factory) == after
    restarted = create_app(FaultController(), lab_release_store=LabReleaseStore(factory))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=restarted), base_url="http://lab"
    ) as client:
        assert (await client.get(path)).json()["record"] == result["record"]


async def test_cancel_is_terminal_and_old_put_cannot_bypass_same_key(database_session_factory):
    factory = database_session_factory
    app = await make_app(factory)
    value = await body(factory)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://lab"
    ) as client:
        path = PATH + "/" + str(value.operation_id)
        assert (await client.post(PATH, json=value.model_dump(mode="json"))).status_code == 201
        cancelled = await client.delete(path)
        assert cancelled.status_code == 200
        assert cancelled.json()["dispatch"]["availability"] == "cancelled"
        before = await snapshot(factory)
        assert (await client.delete(path)).json()["applied"] is False
        assert (await client.put(path)).status_code == 409
        old = await client.put(
            "/__admin/simulated-release", json=value.command.model_dump(mode="json")
        )
        assert old.status_code == 503
        assert (await client.post(PATH, json=value.model_dump(mode="json"))).status_code == 200
        assert await snapshot(factory) == before


async def test_concurrent_duplicate_and_cancel_application_are_single_winner(
    database_session_factory,
):
    factory = database_session_factory
    app = await make_app(factory)
    value = await body(factory)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://lab"
    ) as client:
        path = PATH + "/" + str(value.operation_id)
        registered = await asyncio.gather(
            *(client.post(PATH, json=value.model_dump(mode="json")) for _ in range(3))
        )
        assert sorted(r.status_code for r in registered) == [200, 200, 201]
        executed, cancelled = await asyncio.gather(client.put(path), client.delete(path))
        assert (executed.status_code, cancelled.status_code) in {(201, 409), (409, 200)}
        data = (await client.get(path)).json()
        assert data["record"]["status"] in {"applied", "cancelled"}
        state = await snapshot(factory)
        assert state[1] == (2 if data["record"]["status"] == "applied" else 1)


async def test_lock_wait_does_not_extend_deadline_and_expired_cannot_be_renewed(
    database_session_factory,
):
    factory = database_session_factory
    app = await make_app(factory)
    value = await body(factory, seconds=0.8)
    store = app.state.lab_release_store.dispatches
    await store.register(value)
    before = await snapshot(factory)
    async with factory() as blocker, blocker.begin():
        await blocker.execute(text("SELECT * FROM lab_release_states FOR UPDATE"))
        pending = asyncio.create_task(store.execute(value.operation_id))
        await asyncio.sleep(0)
        await wait_past(blocker, value.expires_at)
        assert not pending.done()
    with pytest.raises(ReleaseError, match="窗口"):
        await pending
    assert (await store.get(value.operation_id)).availability == "expired"
    replay = await store.register(value)
    assert not replay.applied and replay.dispatch.availability == "expired"
    # 即使迟到请求伪称在窗口内创建，也必须由数据库当前时钟拒绝。
    with pytest.raises(DBAPIError):
        async with factory() as session, session.begin():
            await native_insert(
                session, value, created_at=value.expires_at - timedelta(microseconds=1)
            )
    assert await snapshot(factory) == before


async def test_deferred_deadline_rolls_back_command_state_and_dispatch_together(
    database_session_factory,
):
    factory = database_session_factory
    app = await make_app(factory)
    value = await body(factory, seconds=2)
    await app.state.lab_release_store.dispatches.register(value)
    before = await snapshot(factory)
    with pytest.raises(DBAPIError, match="截止"):
        async with factory() as session, session.begin():
            await native_insert(session, value)
            assert (
                await session.scalar(text("SELECT status FROM lab_release_dispatches")) == "applied"
            )
            await wait_past(session, value.expires_at)
    assert await snapshot(factory) == before
    assert (
        await app.state.lab_release_store.dispatches.get(value.operation_id)
    ).availability == "expired"


async def test_native_forgery_bypass_immutability_savepoint_and_downgrade_guards(
    database_session_factory,
):
    factory = database_session_factory
    app = await make_app(factory)
    value = await body(factory)
    with pytest.raises(DBAPIError):
        async with factory() as session, session.begin():
            await native_insert(session, value)
    store = app.state.lab_release_store.dispatches
    await store.register(value)
    for sql in (
        "UPDATE lab_release_dispatches SET expires_at=expires_at+interval '1 second'",
        "UPDATE lab_release_dispatches SET command_sha256=repeat('0',64)",
        "UPDATE lab_release_dispatches SET status='applied',"
        "command_id=(SELECT id FROM lab_release_commands LIMIT 1),finished_at=clock_timestamp()",
        "DELETE FROM lab_release_dispatches",
    ):
        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(text(sql))
    with pytest.raises(Exception, match="不能删除"):
        await asyncio.to_thread(
            alembic_command.downgrade, config(), "0080_candidate_release_request"
        )
    async with factory() as session, session.begin(), session.begin_nested():
        await native_insert(session, value)
    view = await store.get(value.operation_id)
    assert view.availability == "applied" and view.receipt.effect_current


async def test_lost_response_uses_original_readable_fact_without_new_effect(
    database_session_factory,
):
    factory = database_session_factory
    app = await make_app(factory)
    value = await body(factory)
    store = app.state.lab_release_store.dispatches
    await store.register(value)

    class Lost(httpx2.AsyncBaseTransport):
        async def handle_async_request(self, request):
            response = await httpx2.ASGITransport(app=app).handle_async_request(request)
            assert response.status_code == 201
            await response.aclose()
            raise httpx2.ReadError("响应丢失", request=request)

    async with httpx2.AsyncClient(transport=Lost(), base_url="http://lab") as client:
        with pytest.raises(httpx2.ReadError):
            await client.put(PATH + "/" + str(value.operation_id))
    before = await snapshot(factory)
    view = await store.get(value.operation_id)
    assert view.record.status == "applied" and view.receipt.command.command == value.command
    assert await snapshot(factory) == before


async def test_corrupt_fact_never_becomes_replay_or_cancel_success(database_session_factory):
    factory = database_session_factory
    app = await make_app(factory)
    value = await body(factory)
    store = app.state.lab_release_store.dispatches
    await store.register(value)
    async with factory() as session, session.begin():
        await session.execute(text("SET LOCAL session_replication_role='replica'"))
        await session.execute(
            text("UPDATE lab_release_dispatches SET command_sha256=repeat('0',64)")
        )
    before = await snapshot(factory)
    for action in (
        lambda: store.get(value.operation_id),
        lambda: store.register(value),
        lambda: store.execute(value.operation_id),
        lambda: store.cancel(value.operation_id),
    ):
        with pytest.raises(ReleaseError, match="重建"):
            await action()
    assert await snapshot(factory) == before
