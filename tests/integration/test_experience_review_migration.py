"""真实 PostgreSQL 验证审核关系约束、不可变事实和安全降级。"""

import asyncio
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command
from tests.integration.test_experience_migration import (
    BASE_TIME,
    ReferenceIds,
    _candidate,
    _job,
    _references,
    config,
)

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def _candidate_reference(session: AsyncSession) -> tuple[ReferenceIds, UUID]:
    """复用最小原生外键行；这些行不是经过业务校验的经验来源。"""
    ids = await _references(session)
    await _job(session, ids)
    return ids, await _candidate(session, ids)


async def _review(
    session: AsyncSession, ids: ReferenceIds, candidate_id: UUID, **overrides: Any
) -> UUID:
    review_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO experience_reviews (
                id, candidate_id, candidate_version, candidate_content_hash, source_hash,
                review_version, policy_version, decision, reviewed_by_id, authorization_id,
                idempotency_key, command_hash, reason, reviewed_at
            ) VALUES (
                :id, :candidate_id, :candidate_version, :candidate_content_hash, :source_hash,
                :review_version, :policy_version, :decision, :reviewed_by_id, :authorization_id,
                :idempotency_key, :command_hash, :reason, :reviewed_at
            )
        """),
        {
            "id": review_id,
            "candidate_id": candidate_id,
            "candidate_version": 1,
            "candidate_content_hash": "d" * 64,
            "source_hash": "c" * 64,
            "review_version": 1,
            "policy_version": "experience-review-v1",
            "decision": "confirm",
            "reviewed_by_id": "trusted-engineer",
            "authorization_id": ids.authorization_id,
            "idempotency_key": "review-key-001",
            "command_hash": "e" * 64,
            "reason": "核对原始候选及来源后确认",
            "reviewed_at": BASE_TIME,
            **overrides,
        },
    )
    return review_id


async def _entry(
    session: AsyncSession, candidate_id: UUID, review_id: UUID, **overrides: Any
) -> UUID:
    entry_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO experience_entries (
                id, review_id, candidate_id, candidate_version, candidate_content_hash,
                source_hash, review_decision, version, status, content, content_hash, created_at
            ) VALUES (
                :id, :review_id, :candidate_id, :candidate_version, :candidate_content_hash,
                :source_hash, :review_decision, :version, :status,
                CAST(:content AS jsonb), :content_hash, :created_at
            )
        """),
        {
            "id": entry_id,
            "review_id": review_id,
            "candidate_id": candidate_id,
            "candidate_version": 1,
            "candidate_content_hash": "d" * 64,
            "source_hash": "c" * 64,
            "review_decision": "confirm",
            "version": 1,
            "status": "confirmed",
            "content": "{}",
            "content_hash": "f" * 64,
            "created_at": BASE_TIME,
            **overrides,
        },
    )
    return entry_id


async def _command_result(
    session: AsyncSession, ids: ReferenceIds, candidate_id: UUID, **overrides: Any
) -> UUID:
    command_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO experience_review_commands (
                id, authorization_id, candidate_id, requested_by_id, command, command_hash,
                outcome, error_code, http_status, review_id, created_at
            ) VALUES (
                :id, :authorization_id, :candidate_id, :requested_by_id,
                CAST(:command AS jsonb), :command_hash, :outcome, :error_code,
                :http_status, :review_id, :created_at
            )
        """),
        {
            "id": command_id,
            "authorization_id": ids.authorization_id,
            "candidate_id": candidate_id,
            "requested_by_id": "trusted-engineer",
            "command": "{}",
            "command_hash": "e" * 64,
            "outcome": "error",
            "error_code": "candidate_not_found",
            "http_status": 404,
            "review_id": None,
            "created_at": BASE_TIME,
            **overrides,
        },
    )
    return command_id


async def test_0063_empty_review_tables_can_downgrade_and_restore_without_changing_candidate(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        _, candidate_id = await _candidate_reference(session)
        before = await session.scalar(
            text("SELECT to_jsonb(c) FROM experience_candidates c WHERE id = :id"),
            {"id": candidate_id},
        )
    await asyncio.to_thread(command.downgrade, config(), "0062_experience_candidates")
    try:
        async with database_session_factory() as session:
            for table in ("experience_reviews", "experience_entries", "experience_review_commands"):
                assert (
                    await session.scalar(text("SELECT to_regclass(:table)"), {"table": table})
                    is None
                )
            assert (
                await session.scalar(
                    text("SELECT to_jsonb(c) FROM experience_candidates c WHERE id = :id"),
                    {"id": candidate_id},
                )
                == before
            )
            assert (
                await session.scalar(
                    text("SELECT count(*) FROM pg_constraint WHERE conname = :name"),
                    {"name": "uq_experience_candidate_identity"},
                )
                == 0
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
    await asyncio.to_thread(command.check, config())


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"candidate_version": 0}, "ck_experience_reviews_versions"),
        ({"review_version": 2}, "ck_experience_reviews_versions"),
        ({"policy_version": "unknown-policy"}, "ck_experience_reviews_policy"),
        ({"decision": "approve"}, "ck_experience_reviews_decision"),
        ({"candidate_content_hash": "D" * 64}, "ck_experience_reviews_hashes"),
        ({"reviewed_by_id": " "}, "ck_experience_reviews_text"),
        ({"reason": " "}, "ck_experience_reviews_text"),
        ({"idempotency_key": "short"}, "ck_experience_reviews_idempotency_key"),
        ({"idempotency_key": "中文审核请求123"}, "ck_experience_reviews_idempotency_key"),
        ({"candidate_version": 2}, "fk_experience_reviews_candidate_identity"),
        ({"candidate_content_hash": "a" * 64}, "fk_experience_reviews_candidate_identity"),
        ({"source_hash": "a" * 64}, "fk_experience_reviews_candidate_identity"),
    ],
)
async def test_0063_review_shape_and_exact_candidate_reference(
    database_session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, candidate_id = await _candidate_reference(session)
        with pytest.raises(DBAPIError, match=constraint):
            async with session.begin_nested():
                await _review(session, ids, candidate_id, **overrides)


async def test_0063_candidate_can_have_only_one_review(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, candidate_id = await _candidate_reference(session)
        await _review(session, ids, candidate_id)
        other_ids = await _references(session)
        with pytest.raises(DBAPIError, match="uq_experience_reviews_candidate"):
            async with session.begin_nested():
                await _review(session, other_ids, candidate_id, decision="reject")


async def test_0063_review_authorization_cannot_be_reused_for_another_candidate(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        first_ids, first_candidate = await _candidate_reference(session)
        second_ids, second_candidate = await _candidate_reference(session)
        await _review(session, first_ids, first_candidate)
        with pytest.raises(DBAPIError, match="uq_experience_reviews_authorization"):
            async with session.begin_nested():
                await _review(
                    session,
                    second_ids,
                    second_candidate,
                    authorization_id=first_ids.authorization_id,
                )


@pytest.mark.parametrize("decision", ["confirm", "reject"])
async def test_0063_entry_requires_a_confirmation_not_a_rejection(
    database_session_factory: async_sessionmaker[AsyncSession], decision: str
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, candidate_id = await _candidate_reference(session)
        review_id = await _review(session, ids, candidate_id, decision=decision)
        if decision == "confirm":
            entry_id = await _entry(session, candidate_id, review_id)
            assert (
                await session.scalar(
                    text("SELECT status FROM experience_entries WHERE id = :id"), {"id": entry_id}
                )
                == "confirmed"
            )
        else:
            with pytest.raises(DBAPIError, match="fk_experience_entries_confirmed_review"):
                async with session.begin_nested():
                    await _entry(session, candidate_id, review_id)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"version": 2}, "ck_experience_entries_version_status"),
        ({"status": "pending_review"}, "ck_experience_entries_version_status"),
        ({"review_decision": "reject"}, "ck_experience_entries_version_status"),
        ({"content_hash": "invalid"}, "ck_experience_entries_hashes"),
        ({"content": "[]"}, "ck_experience_entries_content"),
        ({"candidate_content_hash": "a" * 64}, "fk_experience_entries_confirmed_review"),
        ({"source_hash": "a" * 64}, "fk_experience_entries_confirmed_review"),
    ],
)
async def test_0063_confirmed_entry_cannot_cross_review_identity(
    database_session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, candidate_id = await _candidate_reference(session)
        review_id = await _review(session, ids, candidate_id)
        with pytest.raises(DBAPIError, match=constraint):
            async with session.begin_nested():
                await _entry(session, candidate_id, review_id, **overrides)


async def test_0063_review_has_at_most_one_confirmed_version(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, candidate_id = await _candidate_reference(session)
        review_id = await _review(session, ids, candidate_id)
        await _entry(session, candidate_id, review_id)
        with pytest.raises(DBAPIError, match="uq_experience_entries_(review|candidate)"):
            async with session.begin_nested():
                await _entry(session, candidate_id, review_id)


@pytest.mark.parametrize(("outcome", "http_status"), [("applied", 201), ("replayed", 200)])
async def test_0063_successful_command_requires_exact_outcome_and_review(
    database_session_factory: async_sessionmaker[AsyncSession], outcome: str, http_status: int
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, candidate_id = await _candidate_reference(session)
        review_id = await _review(session, ids, candidate_id)
        command_id = await _command_result(
            session,
            ids,
            candidate_id,
            outcome=outcome,
            error_code=None,
            http_status=http_status,
            review_id=review_id,
        )
        assert (
            await session.scalar(
                text("SELECT http_status FROM experience_review_commands WHERE id = :id"),
                {"id": command_id},
            )
            == http_status
        )


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"command": "[]"}, "ck_experience_review_commands_command"),
        ({"command_hash": "bad-hash"}, "ck_experience_review_commands_hash"),
        ({"requested_by_id": " "}, "ck_experience_review_commands_subject"),
        ({"error_code": "secret=unsafe"}, "ck_experience_review_commands_error_code"),
        ({"outcome": "applied", "http_status": 201}, "ck_experience_review_commands_outcome"),
        ({"outcome": "replayed", "http_status": 200}, "ck_experience_review_commands_outcome"),
        ({"error_code": None}, "ck_experience_review_commands_outcome"),
        ({"http_status": 500}, "ck_experience_review_commands_outcome"),
    ],
)
async def test_0063_command_result_shape_is_enforced(
    database_session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        with pytest.raises(DBAPIError, match=constraint):
            async with session.begin_nested():
                await _command_result(session, ids, uuid4(), **overrides)


async def test_0063_unknown_candidate_error_is_auditable_but_authorization_is_unique(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        unknown_candidate = uuid4()
        command_id = await _command_result(session, ids, unknown_candidate)
        assert (
            await session.scalar(
                text("SELECT candidate_id FROM experience_review_commands WHERE id = :id"),
                {"id": command_id},
            )
            == unknown_candidate
        )
        with pytest.raises(DBAPIError, match="uq_experience_review_commands_authorization"):
            async with session.begin_nested():
                await _command_result(session, ids, unknown_candidate)


async def test_0063_review_entry_and_business_result_are_immutable(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, candidate_id = await _candidate_reference(session)
        review_id = await _review(session, ids, candidate_id)
        entry_id = await _entry(session, candidate_id, review_id)
        command_id = await _command_result(
            session,
            ids,
            candidate_id,
            outcome="applied",
            error_code=None,
            http_status=201,
            review_id=review_id,
        )
        for table, record_id, field in (
            ("experience_reviews", review_id, "reason"),
            ("experience_entries", entry_id, "content"),
            ("experience_review_commands", command_id, "outcome"),
        ):
            for statement in (
                f"UPDATE {table} SET {field} = {field} WHERE id = :id",
                f"DELETE FROM {table} WHERE id = :id",
            ):
                with pytest.raises(
                    DBAPIError, match="经验审核决定、确认版本和业务结果不可修改或删除"
                ):
                    async with session.begin_nested():
                        await session.execute(text(statement), {"id": record_id})


@pytest.mark.parametrize("fact", ["review", "unknown_candidate_command"])
async def test_0063_any_new_fact_prevents_downgrade_and_remains_readable(
    database_session_factory: async_sessionmaker[AsyncSession], fact: str
) -> None:
    async with database_session_factory() as session, session.begin():
        ids, candidate_id = await _candidate_reference(session)
        if fact == "review":
            record_id = await _review(session, ids, candidate_id)
            table = "experience_reviews"
        else:
            record_id = await _command_result(session, ids, uuid4())
            table = "experience_review_commands"
    with pytest.raises(DBAPIError, match="不能删除已持久化的经验审核或确认事实"):
        await asyncio.to_thread(command.downgrade, config(), "0062_experience_candidates")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0091_reflow_review"
        )
        assert (
            await session.scalar(text(f"SELECT id FROM {table} WHERE id = :id"), {"id": record_id})
            == record_id
        )
