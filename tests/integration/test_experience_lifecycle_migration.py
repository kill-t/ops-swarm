"""真实 PostgreSQL 验证停用约束、范围索引和治理事实保护。"""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from tests.integration.test_experience_migration import BASE_TIME, ReferenceIds, _references, config
from tests.integration.test_experience_review_migration import _candidate_reference, _entry, _review

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _confirmed_entry_reference(session: AsyncSession) -> tuple[ReferenceIds, UUID]:
    """只创建最小原生外键行，不将这些行描述为业务来源验收。"""
    ids, candidate_id = await _candidate_reference(session)
    review_id = await _review(session, ids, candidate_id)
    return ids, await _entry(session, candidate_id, review_id)


async def _disablement(
    session: AsyncSession, ids: ReferenceIds, entry_id: UUID, **overrides: Any
) -> UUID:
    disablement_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO experience_disablements (
                id, entry_id, entry_version, entry_content_hash, source_hash, state_version,
                policy_version, disabled_by_id, authorization_id, idempotency_key,
                command_hash, reason, disabled_at
            ) VALUES (
                :id, :entry_id, :entry_version, :entry_content_hash, :source_hash, :state_version,
                :policy_version, :disabled_by_id, :authorization_id, :idempotency_key,
                :command_hash, :reason, :disabled_at
            )
        """),
        {
            "id": disablement_id,
            "entry_id": entry_id,
            "entry_version": 1,
            "entry_content_hash": "f" * 64,
            "source_hash": "c" * 64,
            "state_version": 2,
            "policy_version": "experience-lifecycle-v1",
            "disabled_by_id": "trusted-engineer",
            "authorization_id": ids.authorization_id,
            "idempotency_key": "disable-command-001",
            "command_hash": "e" * 64,
            "reason": "历史材料已不再适用",
            "disabled_at": BASE_TIME,
            **overrides,
        },
    )
    return disablement_id


async def _lifecycle_result(
    session: AsyncSession, ids: ReferenceIds, entry_id: UUID, **overrides: Any
) -> UUID:
    command_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO experience_lifecycle_commands (
                id, authorization_id, entry_id, requested_by_id, command, command_hash,
                outcome, error_code, http_status, disablement_id, created_at
            ) VALUES (
                :id, :authorization_id, :entry_id, :requested_by_id, CAST(:command AS jsonb),
                :command_hash, :outcome, :error_code, :http_status, :disablement_id, :created_at
            )
        """),
        {
            "id": command_id,
            "authorization_id": ids.authorization_id,
            "entry_id": entry_id,
            "requested_by_id": "trusted-engineer",
            "command": "{}",
            "command_hash": "e" * 64,
            "outcome": "error",
            "error_code": "experience_entry_not_found",
            "http_status": 404,
            "disablement_id": None,
            "created_at": BASE_TIME,
            **overrides,
        },
    )
    return command_id


async def test_0064_empty_governance_tables_downgrade_without_rewriting_entry(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        _, entry_id = await _confirmed_entry_reference(session)
        before = await session.scalar(
            text("SELECT to_jsonb(e) FROM experience_entries e WHERE id = :id"), {"id": entry_id}
        )
    await asyncio.to_thread(command.downgrade, config(), "0063_trusted_experience_review")
    try:
        async with database_session_factory() as session:
            for table in ("experience_disablements", "experience_lifecycle_commands"):
                assert (
                    await session.scalar(text("SELECT to_regclass(:table)"), {"table": table})
                    is None
                )
            assert (
                await session.scalar(
                    text("SELECT to_jsonb(e) FROM experience_entries e WHERE id = :id"),
                    {"id": entry_id},
                )
                == before
            )
            assert (
                await session.scalar(text("SELECT to_regclass('ix_experience_entries_scope')"))
                is None
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = :name"),
                    {"name": "uq_experience_entry_identity"},
                )
                == 0
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
    await asyncio.to_thread(command.check, config())


async def test_0064_scope_index_contains_three_json_scope_fields_and_stable_order(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session:
        definition = await session.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_experience_entries_scope'")
        )
        assert definition is not None
        for expression in ("candidate_snapshot", "service_name", "environment", "data_source_mode"):
            assert expression in definition
        assert "created_at DESC" in definition
        assert (
            await session.scalar(
                text("""
                SELECT indnkeyatts FROM pg_index
                WHERE indexrelid = 'ix_experience_entries_scope'::regclass
            """)
            )
            == 5
        )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"entry_version": 2}, "ck_experience_disablements_versions"),
        ({"state_version": 1}, "ck_experience_disablements_versions"),
        ({"policy_version": "unknown"}, "ck_experience_disablements_policy"),
        ({"entry_content_hash": "F" * 64}, "ck_experience_disablements_hashes"),
        ({"disabled_by_id": " "}, "ck_experience_disablements_text"),
        ({"reason": " "}, "ck_experience_disablements_text"),
        ({"idempotency_key": "short"}, "ck_experience_disablements_idempotency_key"),
        ({"idempotency_key": "中文停用请求001"}, "ck_experience_disablements_idempotency_key"),
        ({"entry_content_hash": "a" * 64}, "fk_experience_disablements_entry_identity"),
        ({"source_hash": "a" * 64}, "fk_experience_disablements_entry_identity"),
    ],
)
async def test_0064_disablement_shape_and_exact_entry_are_enforced(
    database_session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id = await _confirmed_entry_reference(session)
        with pytest.raises(DBAPIError, match=constraint):
            async with session.begin_nested():
                await _disablement(session, ids, entry_id, **overrides)


async def test_0064_entry_has_only_one_disablement_and_keeps_immutable_confirmed_body(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id = await _confirmed_entry_reference(session)
        before = await session.scalar(
            text("SELECT to_jsonb(e) FROM experience_entries e WHERE id = :id"), {"id": entry_id}
        )
        await _disablement(session, ids, entry_id)
        other_ids = await _references(session)
        with pytest.raises(DBAPIError, match="uq_experience_disablements_entry"):
            async with session.begin_nested():
                await _disablement(session, other_ids, entry_id)
        assert (
            await session.scalar(
                text("SELECT to_jsonb(e) FROM experience_entries e WHERE id = :id"),
                {"id": entry_id},
            )
            == before
        )


async def test_0064_authorization_cannot_disable_two_entries(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        first_ids, first_entry = await _confirmed_entry_reference(session)
        second_ids, second_entry = await _confirmed_entry_reference(session)
        await _disablement(session, first_ids, first_entry)
        with pytest.raises(DBAPIError, match="uq_experience_disablements_authorization"):
            async with session.begin_nested():
                await _disablement(
                    session, second_ids, second_entry, authorization_id=first_ids.authorization_id
                )


@pytest.mark.parametrize(("outcome", "http_status"), [("applied", 201), ("replayed", 200)])
async def test_0064_successful_outcome_binds_disablement(
    database_session_factory: async_sessionmaker[AsyncSession], outcome: str, http_status: int
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id = await _confirmed_entry_reference(session)
        disablement_id = await _disablement(session, ids, entry_id)
        command_id = await _lifecycle_result(
            session,
            ids,
            entry_id,
            outcome=outcome,
            error_code=None,
            http_status=http_status,
            disablement_id=disablement_id,
        )
        assert (
            await session.scalar(
                text("SELECT disablement_id FROM experience_lifecycle_commands WHERE id = :id"),
                {"id": command_id},
            )
            == disablement_id
        )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"command": "[]"}, "ck_experience_lifecycle_commands_command"),
        ({"command_hash": "not-a-hash"}, "ck_experience_lifecycle_commands_hash"),
        ({"requested_by_id": " "}, "ck_experience_lifecycle_commands_subject"),
        ({"error_code": "secret=unsafe"}, "ck_experience_lifecycle_commands_error_code"),
        ({"outcome": "applied", "http_status": 201}, "ck_experience_lifecycle_commands_outcome"),
        ({"outcome": "replayed", "http_status": 200}, "ck_experience_lifecycle_commands_outcome"),
        ({"error_code": None}, "ck_experience_lifecycle_commands_outcome"),
        ({"http_status": 500}, "ck_experience_lifecycle_commands_outcome"),
    ],
)
async def test_0064_business_result_shape_is_enforced(
    database_session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        with pytest.raises(DBAPIError, match=constraint):
            async with session.begin_nested():
                await _lifecycle_result(session, ids, uuid4(), **overrides)


async def test_0064_unknown_entry_is_auditable_once_per_authorization(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        unknown_entry = uuid4()
        command_id = await _lifecycle_result(session, ids, unknown_entry)
        assert (
            await session.scalar(
                text("SELECT entry_id FROM experience_lifecycle_commands WHERE id = :id"),
                {"id": command_id},
            )
            == unknown_entry
        )
        with pytest.raises(DBAPIError, match="uq_experience_lifecycle_commands_authorization"):
            async with session.begin_nested():
                await _lifecycle_result(session, ids, unknown_entry)


async def test_0064_disablement_and_business_result_cannot_be_changed_or_deleted(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id = await _confirmed_entry_reference(session)
        disablement_id = await _disablement(session, ids, entry_id)
        command_id = await _lifecycle_result(
            session,
            ids,
            entry_id,
            outcome="applied",
            error_code=None,
            http_status=201,
            disablement_id=disablement_id,
        )
        for table, record_id, field in (
            ("experience_disablements", disablement_id, "reason"),
            ("experience_lifecycle_commands", command_id, "outcome"),
        ):
            for statement in (
                f"UPDATE {table} SET {field} = {field} WHERE id = :id",
                f"DELETE FROM {table} WHERE id = :id",
            ):
                with pytest.raises(
                    DBAPIError, match="经验停用事实和生命周期业务结果不可修改或删除"
                ):
                    async with session.begin_nested():
                        await session.execute(text(statement), {"id": record_id})


@pytest.mark.parametrize("fact", ["disablement", "unknown_entry_command"])
async def test_0064_new_governance_fact_blocks_destructive_downgrade(
    database_session_factory: async_sessionmaker[AsyncSession], fact: str
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, entry_id = await _confirmed_entry_reference(session)
        if fact == "disablement":
            record_id = await _disablement(session, ids, entry_id)
            table = "experience_disablements"
        else:
            record_id = await _lifecycle_result(session, ids, uuid4())
            table = "experience_lifecycle_commands"
    with pytest.raises(DBAPIError, match="不能删除已持久化的经验停用或生命周期事实"):
        await asyncio.to_thread(command.downgrade, config(), "0063_trusted_experience_review")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0069_human_input_preparation"
        )
        assert (
            await session.scalar(text(f"SELECT id FROM {table} WHERE id = :id"), {"id": record_id})
            == record_id
        )
