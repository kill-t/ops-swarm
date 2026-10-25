"""真实PostgreSQL验证发布演练CAS、兼容入口及持久状态失败关闭。"""

import asyncio
from datetime import UTC, datetime
from uuid import UUID

import httpx2
import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.orm import Session

from opsswarm.fault_lab.app import create_app
from opsswarm.fault_lab.controller import FaultController
from opsswarm.fault_lab.release.contracts import ReleaseError
from opsswarm.fault_lab.release.store import LabReleaseStore
from opsswarm.fault_lab.release.tables import LabReleaseCommandRow
from opsswarm.fault_lab.runtime_metadata import RuntimeMetadataSettings

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
NOW = datetime(2026, 10, 25, 14, tzinfo=UTC)
PATH = "/__admin/simulated-release"
BAD_VERSION = "2026.01.01-rc1"


def body(*, version="stable", revision=1, target=BAD_VERSION, key="release-command-123"):
    return dict(
        expected_version=version,
        expected_revision=revision,
        target_version=target,
        idempotency_key=key,
    )


def application(factory, **kwargs):
    return create_app(
        FaultController(),
        lab_release_store=LabReleaseStore(factory, clock=lambda: NOW),
        runtime_clock=lambda: NOW,
        **kwargs,
    )


async def counts(factory):
    async with factory() as session:
        return await session.scalar(text("SELECT count(*) FROM lab_release_commands"))


async def test_explicit_lifecycle_and_multiple_apps_keep_durable_state(database_session_factory):
    factory = database_session_factory
    app = application(factory)
    async with (
        app.router.lifespan_context(app),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://lab"
        ) as client,
    ):
        initial = (await client.get(PATH)).json()
        assert initial == dict(
            service_name="order-service",
            source="local-release-simulation-v1",
            simulation=True,
            version="stable",
            revision=1,
            command_count=0,
            release_500_enabled=False,
            updated_at="2026-10-25T14:00:00Z",
        )
        result = await client.put(PATH, json=body())
        assert result.status_code == 201, result.text
        applied = result.json()
        assert applied["applied"] is True and applied["effect_current"] is True
        assert applied["current"]["version"] == BAD_VERSION
        assert applied["current"]["revision"] == 2
    restarted = application(factory)
    async with (
        restarted.router.lifespan_context(restarted),
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=restarted), base_url="http://lab"
        ) as client,
    ):
        assert (await client.get(PATH)).json() == applied["current"]
        replay = await client.put(PATH, json=body())
        assert replay.status_code == 200
        assert replay.json() == {**applied, "applied": False}
    assert await counts(factory) == 1


async def test_cas_concurrency_aba_and_replay_distinguish_history(database_session_factory):
    factory = database_session_factory
    app, other = application(factory), application(factory)
    await asyncio.gather(
        app.state.lab_release_store.initialize(), other.state.lab_release_store.initialize()
    )
    async with (
        httpx2.AsyncClient(transport=httpx2.ASGITransport(app=app), base_url="http://lab") as first,
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=other), base_url="http://lab"
        ) as second,
    ):
        repeated = await asyncio.gather(first.put(PATH, json=body()), second.put(PATH, json=body()))
        assert sorted(item.status_code for item in repeated) == [200, 201]
        original = next(item.json() for item in repeated if item.status_code == 201)
        changed_key = await first.put(PATH, json=body(target="stable"))
        assert changed_key.status_code == 409
        next_a = body(version=BAD_VERSION, revision=2, target="stable", key="next-first-123")
        next_b = {**next_a, "idempotency_key": "next-second-123"}
        competing = await asyncio.gather(
            first.put(PATH, json=next_a), second.put(PATH, json=next_b)
        )
        assert sorted(item.status_code for item in competing) == [201, 409]
        current = (await first.get(PATH)).json()
        assert current["version"] == "stable" and current["revision"] == 3
        assert current["command_count"] == 2
        assert (await second.put(PATH, json=body(key="aba-stale-revision-123"))).status_code == 409
        assert (
            await second.put(
                PATH, json=body(version=BAD_VERSION, revision=3, key="wrong-version-123")
            )
        ).status_code == 409
        replay = await second.put(PATH, json=body())
        assert replay.status_code == 200
        assert replay.json() == dict(
            applied=False, command=original["command"], current=current, effect_current=False
        )
    assert await counts(factory) == 2


async def test_noop_records_command_without_advancing_revision(database_session_factory):
    app = application(database_session_factory)
    await app.state.lab_release_store.initialize()
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://lab"
    ) as client:
        command = body(target="stable")
        one = await client.put(PATH, json=command)
        two = await client.put(PATH, json={**command, "idempotency_key": "noop-second-123"})
        assert one.status_code == two.status_code == 201
        assert one.json()["current"]["revision"] == two.json()["current"]["revision"] == 1
        assert two.json()["current"]["command_count"] == 2
        replay = await client.put(PATH, json=command)
        assert replay.status_code == 200
        assert replay.json()["command"] == one.json()["command"]
        assert replay.json()["current"] == two.json()["current"]
        assert replay.json()["effect_current"] is True
    assert await counts(database_session_factory) == 2


async def test_legacy_switch_orders_metrics_and_runtime_identity_share_correct_boundaries(
    database_session_factory,
):
    settings = RuntimeMetadataSettings(
        metadata_enabled=True,
        instance_id=str(UUID(int=123)),
        image_repository="ghcr.io/kill-t/ops-swarm-order-service",
        image_digest="sha256:" + "a" * 64,
    )
    app = application(database_session_factory, runtime_metadata_settings=settings)
    peer = application(database_session_factory, runtime_metadata_settings=settings)
    await app.state.lab_release_store.initialize()
    async with (
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://lab"
        ) as client,
        httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=peer), base_url="http://lab"
        ) as other,
    ):
        identity = (await client.get("/api/v1/runtime-metadata")).json()
        inventory = (await client.get("/api/v1/runtime-inventory")).json()
        enabled = await client.put("/__admin/faults/release-500", json={"enabled": True})
        assert enabled.status_code == 200 and enabled.json() == {
            "name": "release_500",
            "enabled": True,
        }
        assert (await other.get("/__admin/faults/release-500")).json() == enabled.json()
        assert (await other.get("/orders/shared-state")).status_code == 500
        assert (
            'opsswarm_fault_lab_http_5xx_ratio{service="order-service"} 1.0'
            in (await other.get("/metrics")).text
        )
        current = (await other.get(PATH)).json()
        assert current["version"] == BAD_VERSION and current["revision"] == 2
        disabled = await other.put("/__admin/faults/release-500", json={"enabled": False})
        assert disabled.json() == {"name": "release_500", "enabled": False}
        assert (await client.get("/orders/shared-state")).status_code == 200
        assert (
            'opsswarm_fault_lab_http_5xx_ratio{service="order-service"} 0.0'
            in (await client.get("/metrics")).text
        )
        await client.put("/__admin/faults/release-500", json={"enabled": False})
        stable = (await other.get(PATH)).json()
        assert stable["revision"] == 3 and stable["command_count"] == 3
        assert (
            await client.put(PATH, json=body(revision=3, key="cas-after-legacy-123"))
        ).status_code == 201
        assert (await other.get("/orders/shared-state")).status_code == 500
        assert (await client.get("/api/v1/runtime-metadata")).json() == identity
        assert (await client.get("/api/v1/runtime-inventory")).json() == inventory
    assert await counts(database_session_factory) == 4


async def test_strict_commands_disabled_mode_and_no_silent_initialization(database_session_factory):
    app = application(database_session_factory)
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://lab"
    ) as client:
        assert (await client.get(PATH)).status_code == 503
        assert await counts(database_session_factory) == 0
        await app.state.lab_release_store.initialize()
        for change in (
            {"expected_revision": True},
            {"expected_revision": 1.0},
            {"expected_revision": "1"},
            {"expected_revision": 0},
            {"expected_revision": 2147483648},
            {"expected_version": "unknown"},
            {"target_version": "latest"},
            {"target_version": " stable "},
            {"idempotency_key": "short"},
            {"service_name": "other-service"},
            {"actor_id": "forged"},
            {"url": "http://elsewhere"},
        ):
            result = await client.put(PATH, json={**body(), **change})
            assert result.status_code == 422, result.text
        assert await counts(database_session_factory) == 0
    legacy = create_app(FaultController())
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=legacy), base_url="http://lab"
    ) as client:
        assert (await client.get(PATH)).status_code == 503
        assert (await client.put(PATH, json=body())).status_code == 503
        assert (await client.get("/orders/default-legacy")).status_code == 200


async def test_database_failure_returns_503_without_legacy_fallback(database_session_factory):
    engine = create_async_engine(
        database_session_factory.kw["bind"].url.set(database="missing_lab_release_123")
    )
    missing = async_sessionmaker(engine, expire_on_commit=False)
    app = application(missing)
    try:
        with pytest.raises(ReleaseError) as failure:
            async with app.router.lifespan_context(app):
                pytest.fail("数据库不可用时不应启动持久演练模式")
        assert failure.value.status_code == 503
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://lab"
        ) as client:
            for path in (PATH, "/__admin/faults/release-500", "/orders/no-fallback", "/metrics"):
                result = await client.get(path)
                assert result.status_code == 503, result.text
            assert (await client.put(PATH, json=body())).status_code == 503
            assert (
                await client.put("/__admin/faults/release-500", json={"enabled": False})
            ).status_code == 503
        assert await counts(database_session_factory) == 0
    finally:
        await engine.dispose()


async def test_write_failure_rolls_back_state_and_command(database_session_factory):
    app = application(database_session_factory)
    await app.state.lab_release_store.initialize()
    raised = False

    def fail_after_flush(session, flush_context):
        nonlocal raised
        if any(isinstance(row, LabReleaseCommandRow) for row in session.new):
            raised = True
            raise SQLAlchemyError("模拟写入后事务失败")

    event.listen(Session, "after_flush", fail_after_flush)
    try:
        async with httpx2.AsyncClient(
            transport=httpx2.ASGITransport(app=app), base_url="http://lab"
        ) as client:
            assert (await client.put(PATH, json=body())).status_code == 503
    finally:
        event.remove(Session, "after_flush", fail_after_flush)
    assert raised and await counts(database_session_factory) == 0
    state = await app.state.lab_release_store.get()
    assert state.version == "stable" and state.revision == 1 and state.command_count == 0


async def test_damaged_current_state_or_latest_command_refuses_reads_and_writes(
    database_session_factory,
):
    factory = database_session_factory
    app = application(factory)
    await app.state.lab_release_store.initialize()
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=app), base_url="http://lab"
    ) as client:
        applied = (await client.put(PATH, json=body())).json()
        for sql, values, restore in (
            ("UPDATE lab_release_states SET revision=:value", {"value": 3}, {"value": 2}),
            (
                "UPDATE lab_release_commands SET command_sha256=:value WHERE id=:id",
                {"value": "f" * 64, "id": UUID(applied["command"]["id"])},
                {
                    "value": applied["command"]["command_sha256"],
                    "id": UUID(applied["command"]["id"]),
                },
            ),
        ):
            try:
                async with factory() as session, session.begin():
                    await session.execute(text("SET LOCAL session_replication_role=replica"))
                    await session.execute(text(sql), values)
                for path in (
                    PATH,
                    "/__admin/faults/release-500",
                    "/orders/no-fallback",
                    "/metrics",
                ):
                    assert (await client.get(path)).status_code == 503
                assert (await client.put(PATH, json=body())).status_code == 503
                assert await counts(factory) == 1
            finally:
                async with factory() as session, session.begin():
                    await session.execute(text("SET LOCAL session_replication_role=replica"))
                    await session.execute(text(sql), restore)
        assert (await client.get(PATH)).json() == applied["current"]


@pytest.mark.parametrize("broken", [False, True])
async def test_environment_enabled_owned_database_is_initialized_and_disposed(
    database_session_factory,
    monkeypatch,
    broken,
):
    from opsswarm.database import Database

    original_dispose = Database.dispose
    disposed = []

    async def dispose(database):
        disposed.append(database)
        await original_dispose(database)

    monkeypatch.setattr(Database, "dispose", dispose)
    monkeypatch.setenv("OPSSWARM_LAB_RELEASE_ENABLED", "true")
    if broken:
        missing = database_session_factory.kw["bind"].url.set(database="missing_owned_release_123")
        monkeypatch.setenv("OPSSWARM_DATABASE_URL", missing.render_as_string(hide_password=False))
    # 不注入Store，验证服务入口实际走环境配置与自有连接池路径。
    app = create_app(FaultController(), runtime_clock=lambda: NOW)
    if broken:
        with pytest.raises(ReleaseError) as failure:
            async with app.router.lifespan_context(app):
                pytest.fail("错误数据库配置不得启动")
        assert failure.value.status_code == 503
    else:
        async with (
            app.router.lifespan_context(app),
            httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app), base_url="http://lab"
            ) as client,
        ):
            assert (await client.get(PATH)).json()["version"] == "stable"
            assert (await client.put(PATH, json=body())).status_code == 201
            assert (await client.get("/orders/configured-state")).status_code == 500
        assert await counts(database_session_factory) == 1
    assert len(disposed) == 1
