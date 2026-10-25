"""隔离PostgreSQL验证发布演练命令与当前状态的原生原子约束。"""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.fault_lab.release.store import LabReleaseStore, command_hash

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
NOW = datetime(2026, 10, 25, 14, tzinfo=UTC)
BAD_VERSION = "2026.01.01-rc1"


def config():
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


def native_command(*, previous="stable", revision=1, sequence=1, target=BAD_VERSION, kind="cas"):
    key = f"native-release-{uuid4()}"
    body = (
        dict(
            expected_version=previous,
            expected_revision=revision,
            target_version=target,
            idempotency_key=key,
        )
        if kind == "cas"
        else dict(enabled=target == BAD_VERSION, idempotency_key=key)
    )
    return dict(
        id=str(uuid4()),
        service_name="order-service",
        sequence=sequence,
        kind=kind,
        idempotency_key=key,
        command=body,
        command_sha256=command_hash(body),
        previous_version=previous,
        previous_revision=revision,
        version=target,
        revision=revision + (target != previous),
        created_at=NOW.isoformat(),
    )


async def insert(session, values):
    await session.execute(
        text(
            "INSERT INTO lab_release_commands SELECT * FROM "
            "jsonb_populate_record(NULL::lab_release_commands,CAST(:values AS jsonb))"
        ),
        {"values": json.dumps(values)},
    )


async def apply(factory, values):
    async with factory() as session, session.begin():
        await insert(session, values)


async def state(factory):
    async with factory() as session:
        return (
            await session.execute(
                text(
                    "SELECT version,revision,command_count,last_command_id,updated_at "
                    "FROM lab_release_states"
                )
            )
        ).one()


async def test_0079_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, config())
    try:
        await asyncio.to_thread(command.downgrade, config(), "0078_condition_approval")
        async with database_session_factory() as session:
            for table in ("lab_release_states", "lab_release_commands"):
                assert (
                    await session.scalar(text("SELECT to_regclass(:name)"), {"name": table}) is None
                )
        await asyncio.to_thread(command.upgrade, config(), "head")
        await asyncio.to_thread(command.check, config())
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")


async def test_0079_initial_state_cannot_be_forged_or_dropped(database_session_factory):
    factory = database_session_factory
    for changed in (
        dict(version=BAD_VERSION),
        dict(revision=2),
        dict(command_count=1),
        dict(service_name="other-service"),
    ):
        values = dict(
            service_name="order-service",
            version="stable",
            revision=1,
            command_count=0,
            last_command_id=None,
            updated_at=NOW.isoformat(),
        )
        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(
                    text(
                        "INSERT INTO lab_release_states SELECT * FROM "
                        "jsonb_populate_record(NULL::lab_release_states,CAST(:values AS jsonb))"
                    ),
                    {"values": json.dumps({**values, **changed})},
                )
    await LabReleaseStore(factory, clock=lambda: NOW).initialize()
    for sql in (
        "UPDATE lab_release_states SET version='2026.01.01-rc1',revision=2",
        "UPDATE lab_release_states SET updated_at=updated_at+interval '1 second'",
        "DELETE FROM lab_release_states",
    ):
        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(text(sql))
    assert (await state(factory))[:3] == ("stable", 1, 0)
    with pytest.raises(Exception, match="不能删除已保存"):
        await asyncio.to_thread(command.downgrade, config(), "0078_condition_approval")


async def test_0079_command_requires_existing_state(database_session_factory):
    with pytest.raises(DBAPIError):
        await apply(database_session_factory, native_command())
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM lab_release_commands")) == 0
        assert await session.scalar(text("SELECT count(*) FROM lab_release_states")) == 0


@pytest.mark.parametrize(
    "change",
    [
        {"sequence": 2},
        {"previous_version": BAD_VERSION},
        {"previous_revision": 2},
        {"version": "latest"},
        {"revision": 3},
        {"command_sha256": "f" * 64},
        {"idempotency_key": "another-key-123"},
        {"created_at": (NOW - timedelta(seconds=1)).isoformat()},
    ],
)
async def test_0079_command_requires_exact_prior_hash_sequence_and_time(
    database_session_factory, change
):
    factory = database_session_factory
    await LabReleaseStore(factory, clock=lambda: NOW).initialize()
    with pytest.raises(DBAPIError):
        await apply(factory, {**native_command(), **change})
    assert (await state(factory))[:3] == ("stable", 1, 0)


async def test_0079_native_command_contract_rejects_coercion_and_unknown_fields(
    database_session_factory,
):
    factory = database_session_factory
    await LabReleaseStore(factory, clock=lambda: NOW).initialize()
    for change in (
        {"expected_revision": True},
        {"expected_revision": 1.0},
        {"expected_revision": "1"},
        {"expected_version": BAD_VERSION},
        {"target_version": "latest"},
        {"actor_id": "forged"},
        {"idempotency_key": 123},
    ):
        values = native_command()
        values["command"] = {**values["command"], **change}
        values["command_sha256"] = command_hash(values["command"])
        with pytest.raises(DBAPIError):
            await apply(factory, values)
    for enabled in (1, "true", None):
        values = native_command(kind="legacy_fault_switch")
        values["command"]["enabled"] = enabled
        values["command_sha256"] = command_hash(values["command"])
        with pytest.raises(DBAPIError):
            await apply(factory, values)
    assert (await state(factory))[:3] == ("stable", 1, 0)


async def test_0079_command_atomically_advances_state_and_rollback_leaves_no_effect(
    database_session_factory,
):
    factory = database_session_factory
    store = LabReleaseStore(factory, clock=lambda: NOW)
    await store.initialize()
    try:
        async with factory() as session, session.begin():
            await insert(session, native_command())
            inside = (
                await session.execute(
                    text("SELECT version,revision,command_count FROM lab_release_states")
                )
            ).one()
            assert inside == (BAD_VERSION, 2, 1)
            raise RuntimeError("模拟外层事务失败")
    except RuntimeError:
        pass
    assert (await state(factory))[:3] == ("stable", 1, 0)
    async with factory() as session:
        assert await session.scalar(text("SELECT count(*) FROM lab_release_commands")) == 0
    first = native_command()
    await apply(factory, first)
    await apply(
        factory, native_command(previous=BAD_VERSION, revision=2, sequence=2, target=BAD_VERSION)
    )
    await apply(
        factory,
        native_command(
            previous=BAD_VERSION,
            revision=2,
            sequence=3,
            target="stable",
            kind="legacy_fault_switch",
        ),
    )
    result = await store.get()
    assert result.version == "stable" and result.revision == 3 and result.command_count == 3
    # 旧命令即使同一事务内再次推进也不能伪造相邻序号。
    for sql in (
        "UPDATE lab_release_states SET version='2026.01.01-rc1',revision=2",
        "UPDATE lab_release_commands SET command='{}'::jsonb",
        "DELETE FROM lab_release_commands",
    ):
        with pytest.raises(DBAPIError):
            async with factory() as session, session.begin():
                await session.execute(text(sql))
    with pytest.raises(Exception, match="不能删除已保存"):
        await asyncio.to_thread(command.downgrade, config(), "0078_condition_approval")


async def test_0079_native_concurrent_cas_has_one_winner(database_session_factory):
    factory = database_session_factory
    await LabReleaseStore(factory, clock=lambda: NOW).initialize()
    results = await asyncio.gather(
        apply(factory, native_command()), apply(factory, native_command()), return_exceptions=True
    )
    assert sum(item is None for item in results) == 1
    assert sum(isinstance(item, DBAPIError) for item in results) == 1
    assert (await state(factory))[:3] == (BAD_VERSION, 2, 1)


async def test_0079_savepoint_keeps_atomic_state_and_command(database_session_factory):
    factory = database_session_factory
    await LabReleaseStore(factory, clock=lambda: NOW).initialize()
    async with factory() as session, session.begin():
        nested = await session.begin_nested()
        await insert(session, native_command())
        assert await session.scalar(text("SELECT revision FROM lab_release_states")) == 2
        await nested.rollback()
        assert await session.scalar(text("SELECT revision FROM lab_release_states")) == 1
        assert await session.scalar(text("SELECT count(*) FROM lab_release_commands")) == 0
        async with session.begin_nested():
            await insert(session, native_command())
    assert (await state(factory))[:3] == (BAD_VERSION, 2, 1)
