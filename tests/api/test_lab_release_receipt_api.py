"""真实PostgreSQL及实际演练ASGI的只读回执与稳定快照。"""

import asyncio
from datetime import UTC, datetime, timedelta

import httpx2
import pytest
from sqlalchemy import text

from opsswarm.execution.release.receipt_client import FaultLabReceiptClient
from opsswarm.execution.release.reconciliation import reconcile_receipt
from opsswarm.fault_lab.app import create_app
from opsswarm.fault_lab.controller import FaultController
from opsswarm.fault_lab.release import store as store_module
from opsswarm.fault_lab.release.contracts import ReleaseCommand
from opsswarm.fault_lab.release.store import LabReleaseStore

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
NOW = datetime(2026, 10, 26, 4, tzinfo=UTC)
PATH = "/__admin/simulated-release"
BAD = "2026.01.01-rc1"


def command(version="stable", revision=1, target=BAD, key="receipt-command-125"):
    return ReleaseCommand(
        expected_version=version,
        expected_revision=revision,
        target_version=target,
        idempotency_key=key,
    )


def app_for(factory):
    return create_app(
        FaultController(),
        lab_release_store=LabReleaseStore(factory, clock=lambda: NOW),
        runtime_clock=lambda: NOW,
    )


async def snapshot(factory):
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT version,revision,command_count,last_command_id,updated_at "
                    "FROM lab_release_states"
                )
            )
        ).all(), await session.scalar(text("SELECT count(*) FROM lab_release_commands"))


async def test_unknown_receipt_does_not_initialize_or_write_and_disabled_is_unavailable(
    database_session_factory,
):
    factory = database_session_factory
    app = app_for(factory)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://lab"
    ) as http:
        assert (await http.get(PATH + "/commands/unknown-key-125")).status_code == 503
        assert await snapshot(factory) == ([], 0)
        await app.state.lab_release_store.initialize()
        before = await snapshot(factory)
        missing = await http.get(PATH + "/commands/unknown-key-125")
        assert missing.status_code == 404
        assert missing.headers["cache-control"] == "no-store"
        assert missing.json()["status"] == "not_found"
        assert missing.json()["command"] is None and missing.json()["simulation"] is True
        assert missing.json()["effect_current"] is False
        assert await snapshot(factory) == before
        for key in ("short", "x" * 101, "bad%20key-125"):
            assert (await http.get(PATH + "/commands/" + key)).status_code == 422
    disabled = create_app(FaultController(), runtime_clock=lambda: NOW)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=disabled), base_url="http://lab"
    ) as http:
        assert (await http.get(PATH + "/commands/unknown-key-125")).status_code == 503


async def test_receipts_survive_restart_and_distinguish_current_noop_aba_and_legacy(
    database_session_factory,
):
    factory = database_session_factory
    app = app_for(factory)
    await app.state.lab_release_store.initialize()
    installed = command()
    rolled = command(BAD, 2, "stable", "candidate-release-operation-125")
    await app.state.lab_release_store.change(installed)
    applied = await app.state.lab_release_store.change(rolled)
    restart = app_for(factory)
    calls = []

    class ReadOnly(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.actual = httpx2.ASGITransport(app=restart)

        async def handle_async_request(self, request):
            calls.append((request.method, request.url.path))
            assert request.method == "GET"
            return await self.actual.handle_async_request(request)

        async def aclose(self):
            await self.actual.aclose()

    reader = FaultLabReceiptClient(transport=ReadOnly(), clock=lambda: NOW)
    before = await snapshot(factory)
    for _ in range(2):
        observed = await reader.get(rolled.idempotency_key)
        assert observed.receipt.command == applied.command
        assert (
            reconcile_receipt(
                rolled, observed, not_before=NOW - timedelta(seconds=1), now=NOW
            ).status
            == "matched_current"
        )
    assert await snapshot(factory) == before
    assert len(calls) == 2
    await restart.state.lab_release_store.change(command("stable", 3, "stable", "later-noop-125"))
    assert (await reader.get(rolled.idempotency_key)).receipt.effect_current is True
    await restart.state.lab_release_store.change(command("stable", 3, BAD, "later-install-125"))
    await restart.state.lab_release_store.change(command(BAD, 4, "stable", "later-rollback-125"))
    aba = await reader.get(rolled.idempotency_key)
    assert aba.receipt.current.version == "stable" and aba.receipt.current.revision == 5
    assert (
        reconcile_receipt(rolled, aba, not_before=NOW - timedelta(seconds=1), now=NOW).status
        == "matched_superseded"
    )
    switched = await restart.state.lab_release_store.set_legacy_fault(enabled=True)
    legacy = await reader.get(switched.command.command.idempotency_key)
    assert legacy.receipt.command.kind == "legacy_fault_switch"
    pretend = command("stable", 5, BAD, switched.command.command.idempotency_key)
    assert (
        reconcile_receipt(pretend, legacy, not_before=NOW - timedelta(seconds=1), now=NOW).status
        == "conflict"
    )


async def test_shared_head_lock_produces_consistent_absence_before_concurrent_write(
    database_session_factory, monkeypatch
):
    factory = database_session_factory
    app = app_for(factory)
    store = app.state.lab_release_store
    await store.initialize()
    entered, release = asyncio.Event(), asyncio.Event()
    original = store_module.read_receipt

    async def paused(session, key, state):
        entered.set()
        await release.wait()
        return await original(session, key, state)

    monkeypatch.setattr(store_module, "read_receipt", paused)
    pending = command()
    reading = asyncio.create_task(store.get_receipt(pending.idempotency_key))
    await asyncio.wait_for(entered.wait(), 2)
    writing = asyncio.create_task(store.change(pending))
    try:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(asyncio.shield(writing), 0.1)
    finally:
        release.set()
    receipt, result = await asyncio.wait_for(asyncio.gather(reading, writing), 5)
    assert receipt.status == "not_found" and receipt.current.command_count == 0
    assert result.applied is True
    assert (await store.get_receipt(pending.idempotency_key)).status == "recorded"


@pytest.mark.parametrize("damage", ["target_hash", "missing_neighbor", "neighbor_hash"])
async def test_damaged_target_or_adjacent_history_is_not_an_absent_or_successful_receipt(
    database_session_factory, damage
):
    factory = database_session_factory
    app = app_for(factory)
    store = app.state.lab_release_store
    await store.initialize()
    first = command()
    await store.change(first)
    await store.change(command(BAD, 2, "stable", "next-receipt-125"))
    await store.change(command("stable", 3, "stable", "noop-receipt-125"))
    async with factory() as session, session.begin():
        await session.execute(text("SET LOCAL session_replication_role='replica'"))
        if damage == "missing_neighbor":
            await session.execute(text("DELETE FROM lab_release_commands WHERE sequence=2"))
        else:
            seq = 1 if damage == "target_hash" else 2
            await session.execute(
                text("UPDATE lab_release_commands SET command_sha256=:bad WHERE sequence=:seq"),
                {"bad": "0" * 64, "seq": seq},
            )
    before = await snapshot(factory)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://lab"
    ) as http:
        response = await http.get(PATH + "/commands/" + first.idempotency_key)
        assert response.status_code == 503, response.text
        assert response.json()["error_code"] == "lab_release_integrity_violation"
    assert await snapshot(factory) == before
