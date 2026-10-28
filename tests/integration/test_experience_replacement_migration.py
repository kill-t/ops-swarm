"""真实 PostgreSQL 验证替代关系、原生终态互斥和不可变治理事实。"""

import asyncio
import json
from datetime import timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from tests.integration.test_experience_lifecycle_migration import _disablement
from tests.integration.test_experience_migration import BASE_TIME, ReferenceIds, _references, config
from tests.integration.test_experience_review_migration import _candidate_reference, _entry, _review

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _entry_reference(
    session: AsyncSession, *, seconds: int = 0, scope: dict[str, Any] | None = None
) -> tuple[ReferenceIds, UUID]:
    """只准备原生约束所需行；不把最小 JSON 冒充业务校验后的来源。"""
    ids, candidate_id = await _candidate_reference(session)
    review_id = await _review(session, ids, candidate_id)
    content = {
        "candidate_snapshot": scope
        if scope is not None
        else {
            "service_name": "order-service",
            "environment": "local_fault_lab",
            "data_source_mode": "frozen",
        }
    }
    entry_id = await _entry(
        session,
        candidate_id,
        review_id,
        content=json.dumps(content),
        created_at=BASE_TIME + timedelta(seconds=seconds),
    )
    return ids, entry_id


async def _pair(session: AsyncSession) -> tuple[ReferenceIds, UUID, ReferenceIds, UUID]:
    ids, entry_id = await _entry_reference(session)
    target_ids, target_id = await _entry_reference(session, seconds=1)
    return ids, entry_id, target_ids, target_id


async def _replacement(
    session: AsyncSession, ids: ReferenceIds, entry_id: UUID, target_id: UUID, **overrides: Any
) -> UUID:
    replacement_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO experience_replacements (
                id, entry_id, entry_version, entry_content_hash, source_hash,
                target_entry_id, target_entry_version, target_content_hash, target_source_hash,
                state_version, policy_version, replaced_by_id, authorization_id,
                idempotency_key, command_hash, reason, replaced_at
            ) VALUES (
                :id, :entry_id, :entry_version, :entry_content_hash, :source_hash,
                :target_entry_id, :target_entry_version, :target_content_hash, :target_source_hash,
                :state_version, :policy_version, :replaced_by_id, :authorization_id,
                :idempotency_key, :command_hash, :reason, :replaced_at
            )
        """),
        {
            "id": replacement_id,
            "entry_id": entry_id,
            "entry_version": 1,
            "entry_content_hash": "f" * 64,
            "source_hash": "c" * 64,
            "target_entry_id": target_id,
            "target_entry_version": 1,
            "target_content_hash": "f" * 64,
            "target_source_hash": "c" * 64,
            "state_version": 2,
            "policy_version": "experience-replacement-v1",
            "replaced_by_id": "trusted-engineer",
            "authorization_id": ids.authorization_id,
            "idempotency_key": "replace-command-001",
            "command_hash": "e" * 64,
            "reason": "采用较新的同范围确认材料",
            "replaced_at": BASE_TIME + timedelta(seconds=10),
            **overrides,
        },
    )
    return replacement_id


async def _result(
    session: AsyncSession, ids: ReferenceIds, entry_id: UUID, target_id: UUID, **overrides: Any
) -> UUID:
    result_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO experience_replacement_commands (
                id, authorization_id, entry_id, target_entry_id, requested_by_id,
                command, command_hash, outcome, error_code, http_status, replacement_id, created_at
            ) VALUES (
                :id, :authorization_id, :entry_id, :target_entry_id, :requested_by_id,
                CAST(:command AS jsonb), :command_hash, :outcome, :error_code,
                :http_status, :replacement_id, :created_at
            )
        """),
        {
            "id": result_id,
            "authorization_id": ids.authorization_id,
            "entry_id": entry_id,
            "target_entry_id": target_id,
            "requested_by_id": "trusted-engineer",
            "command": "{}",
            "command_hash": "e" * 64,
            "outcome": "error",
            "error_code": "experience_entry_not_found",
            "http_status": 404,
            "replacement_id": None,
            "created_at": BASE_TIME + timedelta(seconds=10),
            **overrides,
        },
    )
    return result_id


async def test_0065_empty_tables_restore_old_disablement_semantics_and_keep_entry(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id, _, _ = await _pair(session)
        before = await session.scalar(
            text("SELECT to_jsonb(e) FROM experience_entries e WHERE id = :id"), {"id": entry_id}
        )
    await asyncio.to_thread(command.downgrade, config(), "0064_experience_lifecycle")
    try:
        async with database_session_factory() as session, session.begin():
            for table in ("experience_replacements", "experience_replacement_commands"):
                assert (
                    await session.scalar(text("SELECT to_regclass(:name)"), {"name": table}) is None
                )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM pg_trigger WHERE tgname = :name"),
                    {"name": "experience_disablement_terminal_guard"},
                )
                == 0
            )
            await _disablement(session, ids, entry_id)
            assert (
                await session.scalar(
                    text("SELECT to_jsonb(e) FROM experience_entries e WHERE id = :id"),
                    {"id": entry_id},
                )
                == before
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
    await asyncio.to_thread(command.check, config())


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"entry_version": 2}, "ck_experience_replacements_versions"),
        ({"target_entry_version": 2}, "ck_experience_replacements_versions"),
        ({"state_version": 1}, "ck_experience_replacements_versions"),
        ({"policy_version": "unknown"}, "ck_experience_replacements_policy"),
        ({"entry_content_hash": "F" * 64}, "ck_experience_replacements_hashes"),
        ({"target_source_hash": "C" * 64}, "ck_experience_replacements_hashes"),
        ({"reason": " "}, "ck_experience_replacements_text"),
        ({"replaced_by_id": " "}, "ck_experience_replacements_text"),
        ({"idempotency_key": "short"}, "ck_experience_replacements_idempotency_key"),
        ({"entry_content_hash": "a" * 64}, "fk_experience_replacements_entry_identity"),
        ({"source_hash": "a" * 64}, "fk_experience_replacements_entry_identity"),
        ({"target_content_hash": "a" * 64}, "fk_experience_replacements_target_identity"),
        ({"target_source_hash": "a" * 64}, "fk_experience_replacements_target_identity"),
    ],
)
async def test_0065_exact_identity_and_bounded_shape(
    database_session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id, _, target_id = await _pair(session)
        with pytest.raises(DBAPIError, match=constraint):
            async with session.begin_nested():
                await _replacement(session, ids, entry_id, target_id, **overrides)


async def test_0065_missing_identity_is_left_to_foreign_key_and_self_is_rejected(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id, _, target_id = await _pair(session)
        with pytest.raises(DBAPIError, match="fk_experience_replacements_entry_identity"):
            async with session.begin_nested():
                await _replacement(session, ids, uuid4(), target_id)
        with pytest.raises(DBAPIError, match="fk_experience_replacements_target_identity"):
            async with session.begin_nested():
                await _replacement(session, ids, entry_id, uuid4())
        with pytest.raises(DBAPIError, match="ck_experience_replacements_not_self"):
            async with session.begin_nested():
                await _replacement(session, ids, entry_id, entry_id)


@pytest.mark.parametrize("target_seconds", [-1, 0])
async def test_0065_native_guard_requires_strictly_later_target(
    database_session_factory: async_sessionmaker[AsyncSession],
    target_seconds: int,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id = await _entry_reference(session)
        _, target_id = await _entry_reference(session, seconds=target_seconds)
        with pytest.raises(DBAPIError, match="替代目标确认时间必须严格晚于来源"):
            async with session.begin_nested():
                await _replacement(session, ids, entry_id, target_id)


@pytest.mark.parametrize(
    "scope",
    [
        {},
        {"service_name": None, "environment": "local_fault_lab", "data_source_mode": "frozen"},
        {"service_name": "other", "environment": "local_fault_lab", "data_source_mode": "frozen"},
        {"service_name": "order-service", "environment": "other", "data_source_mode": "frozen"},
        {
            "service_name": "order-service",
            "environment": "local_fault_lab",
            "data_source_mode": "live",
        },
    ],
)
async def test_0065_native_guard_rejects_missing_or_different_scope(
    database_session_factory: async_sessionmaker[AsyncSession],
    scope: dict[str, Any],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id = await _entry_reference(session)
        _, target_id = await _entry_reference(session, seconds=1, scope=scope)
        with pytest.raises(DBAPIError, match="替代双方服务环境和来源范围必须非空一致"):
            async with session.begin_nested():
                await _replacement(session, ids, entry_id, target_id)


async def test_0065_one_target_has_one_predecessor_and_auth_is_not_reused(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, first = await _entry_reference(session)
        second_ids, second = await _entry_reference(session, seconds=1)
        _, target = await _entry_reference(session, seconds=2)
        await _replacement(session, ids, first, target)
        with pytest.raises(DBAPIError, match="uq_experience_replacements_target"):
            async with session.begin_nested():
                await _replacement(session, second_ids, second, target)
        _, last = await _entry_reference(session, seconds=3)
        with pytest.raises(DBAPIError, match="uq_experience_replacements_authorization"):
            async with session.begin_nested():
                await _replacement(
                    session, second_ids, second, last, authorization_id=ids.authorization_id
                )


@pytest.mark.parametrize("disabled_side", ["entry", "target"])
async def test_0065_disablement_before_replacement_blocks_either_endpoint(
    database_session_factory: async_sessionmaker[AsyncSession],
    disabled_side: str,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id, target_ids, target_id = await _pair(session)
        await _disablement(
            session,
            ids if disabled_side == "entry" else target_ids,
            entry_id if disabled_side == "entry" else target_id,
        )
        with pytest.raises(DBAPIError, match="已停用经验不能参与首次替代"):
            async with session.begin_nested():
                await _replacement(session, ids, entry_id, target_id)


async def test_0065_replaced_source_cannot_be_disabled_but_target_can_continue(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id, target_ids, target_id = await _pair(session)
        first_id = await _replacement(session, ids, entry_id, target_id)
        with pytest.raises(DBAPIError, match="已替代来源不能再停用"):
            async with session.begin_nested():
                await _disablement(session, ids, entry_id)
        _, newest = await _entry_reference(session, seconds=2)
        await _replacement(session, target_ids, target_id, newest)
        assert (
            await session.scalar(
                text("SELECT target_entry_id FROM experience_replacements WHERE id = :id"),
                {"id": first_id},
            )
            == target_id
        )


async def test_0065_target_can_be_disabled_after_becoming_successor(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id, target_ids, target_id = await _pair(session)
        await _replacement(session, ids, entry_id, target_id)
        await _disablement(session, target_ids, target_id)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"command": "[]"}, "ck_experience_replacement_commands_command"),
        ({"command_hash": "bad"}, "ck_experience_replacement_commands_hash"),
        ({"requested_by_id": " "}, "ck_experience_replacement_commands_subject"),
        ({"error_code": "secret=value"}, "ck_experience_replacement_commands_error_code"),
        ({"outcome": "applied", "http_status": 201}, "ck_experience_replacement_commands_outcome"),
        ({"error_code": None}, "ck_experience_replacement_commands_outcome"),
    ],
)
async def test_0065_result_shape_and_unknown_endpoints(
    database_session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        with pytest.raises(DBAPIError, match=constraint):
            async with session.begin_nested():
                await _result(session, ids, uuid4(), uuid4(), **overrides)


async def test_0065_facts_and_results_are_immutable_and_unknown_results_are_unique(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id, _, target_id = await _pair(session)
        fact_id = await _replacement(session, ids, entry_id, target_id)
        result_id = await _result(session, ids, uuid4(), uuid4())
        with pytest.raises(DBAPIError, match="uq_experience_replacement_commands_authorization"):
            async with session.begin_nested():
                await _result(session, ids, uuid4(), uuid4())
        for table, record_id, field in (
            ("experience_replacements", fact_id, "reason"),
            ("experience_replacement_commands", result_id, "outcome"),
        ):
            for statement in (
                f"UPDATE {table} SET {field} = {field} WHERE id = :id",
                f"DELETE FROM {table} WHERE id = :id",
            ):
                with pytest.raises(DBAPIError, match="经验替代事实和业务结果不可修改或删除"):
                    async with session.begin_nested():
                        await session.execute(text(statement), {"id": record_id})


@pytest.mark.parametrize("fact", ["replacement", "unknown_command"])
async def test_0065_new_fact_prevents_downgrade_without_losing_data(
    database_session_factory: async_sessionmaker[AsyncSession],
    fact: str,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id, _, target_id = await _pair(session)
        if fact == "replacement":
            record_id = await _replacement(session, ids, entry_id, target_id)
            table = "experience_replacements"
        else:
            record_id = await _result(session, ids, uuid4(), uuid4())
            table = "experience_replacement_commands"
    with pytest.raises(DBAPIError, match="不能删除已持久化的经验替代或结果事实"):
        await asyncio.to_thread(command.downgrade, config(), "0064_experience_lifecycle")
    async with database_session_factory() as session:
        assert (
            await session.scalar(text("SELECT version_num FROM alembic_version"))
            == "0091_reflow_review"
        )
        assert (
            await session.scalar(text(f"SELECT id FROM {table} WHERE id = :id"), {"id": record_id})
            == record_id
        )


async def _wait_for_native_lock(
    factory: async_sessionmaker[AsyncSession],
    pid: int,
) -> None:
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        async with factory() as session:
            waiting = await session.scalar(
                text("SELECT wait_event_type FROM pg_stat_activity WHERE pid = :pid"), {"pid": pid}
            )
        if waiting == "Lock":
            return
        await asyncio.sleep(0.02)
    pytest.fail("原生竞争写入没有进入 PostgreSQL 锁等待")


@pytest.mark.parametrize(
    ("first_operation", "disabled_side", "expected_error"),
    [
        ("disable", "entry", "已停用经验不能参与首次替代"),
        ("replace", "entry", "已替代来源不能再停用"),
        ("disable", "target", "已停用经验不能参与首次替代"),
        ("replace", "target", None),
    ],
)
async def test_0065_native_concurrency_waits_and_rechecks_latest_terminal_fact(
    database_session_factory: async_sessionmaker[AsyncSession],
    first_operation: str,
    disabled_side: str,
    expected_error: str | None,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id, target_ids, target_id = await _pair(session)
    disabled_ids = ids if disabled_side == "entry" else target_ids
    disabled_id = entry_id if disabled_side == "entry" else target_id
    ready = asyncio.Event()
    backend_pid: list[int] = []

    async def competing_insert() -> str | None:
        try:
            async with database_session_factory() as session, session.begin():
                backend_pid.append(await session.scalar(text("SELECT pg_backend_pid()")))
                ready.set()
                if first_operation == "disable":
                    await _replacement(session, ids, entry_id, target_id)
                else:
                    await _disablement(session, disabled_ids, disabled_id)
            return None
        except DBAPIError as error:
            return str(error)

    task = None
    try:
        async with database_session_factory() as session, session.begin():
            if first_operation == "disable":
                await _disablement(session, disabled_ids, disabled_id)
            else:
                await _replacement(session, ids, entry_id, target_id)
            task = asyncio.create_task(competing_insert())
            await asyncio.wait_for(ready.wait(), timeout=5)
            await _wait_for_native_lock(database_session_factory, backend_pid[0])
    finally:
        if task is not None:
            outcome = await asyncio.wait_for(task, timeout=10)
    if expected_error is None:
        assert outcome is None
    else:
        assert outcome is not None and expected_error in outcome
