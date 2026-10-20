"""真实 PostgreSQL 核对经验迁移的约束、不可变事实和降级保护。"""

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from alembic import command

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
BASE_TIME = datetime(2026, 10, 20, tzinfo=UTC)


@dataclass(frozen=True)
class ReferenceIds:
    report_id: UUID
    authorization_id: UUID
    job_id: UUID


def config() -> Config:
    return Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"))


async def _references(session: AsyncSession) -> ReferenceIds:
    """只准备迁移外键所需原生行，不把这些最小行当作业务来源验收。"""
    incident_id = uuid4()
    ids = ReferenceIds(uuid4(), uuid4(), uuid4())
    await session.execute(
        text("""
            INSERT INTO incidents (
                id, service_name, title, severity, status, fingerprint, version,
                started_at, resolved_at, closed_at, created_at, updated_at
            ) VALUES (
                :id, 'order-service', '迁移约束测试', 'p1', 'closed', :fingerprint, 1,
                :at, :at, :at, :at, :at
            )
        """),
        {"id": incident_id, "fingerprint": str(incident_id), "at": BASE_TIME},
    )
    await session.execute(
        text("""
            INSERT INTO incident_reports (
                id, incident_id, incident_version, schema_version, content_hash, content, created_at
            ) VALUES (:id, :incident_id, 1, 'incident-report.v1', :hash, '{}'::jsonb, :at)
        """),
        {"id": ids.report_id, "incident_id": incident_id, "hash": "a" * 64, "at": BASE_TIME},
    )
    await session.execute(
        text("""
            INSERT INTO operator_authorizations (
                id, policy_version, subject_id, credential_id, role, permissions, operation,
                method, route, resources, body_sha256, authorized_at
            ) VALUES (
                :id, 'operator-auth-v1', 'trusted-engineer', 'migration-test', 'engineer',
                '[]'::jsonb, 'request_experience_generation', 'POST', '/migration-test',
                '{}'::jsonb, :hash, :at
            )
        """),
        {"id": ids.authorization_id, "hash": "b" * 64, "at": BASE_TIME},
    )
    return ids


async def _job(session: AsyncSession, ids: ReferenceIds, **overrides: Any) -> None:
    values = {
        "id": ids.job_id,
        "report_id": ids.report_id,
        "policy_version": "report-fact-draft-v1",
        "source": json.dumps({"report_id": str(ids.report_id)}),
        "source_hash": "c" * 64,
        "requested_by_id": "trusted-engineer",
        "authorization_id": ids.authorization_id,
        "status": "queued",
        "attempt_count": 0,
        "max_attempts": 3,
        "available_at": BASE_TIME,
        "lease_token": None,
        "locked_until": None,
        "last_error_code": None,
        "created_at": BASE_TIME,
        "updated_at": BASE_TIME,
        "completed_at": None,
        **overrides,
    }
    await session.execute(
        text("""
            INSERT INTO experience_generation_jobs (
                id, report_id, policy_version, source, source_hash, requested_by_id,
                authorization_id, status, attempt_count, max_attempts, available_at,
                lease_token, locked_until, last_error_code, created_at, updated_at, completed_at
            ) VALUES (
                :id, :report_id, :policy_version, CAST(:source AS jsonb), :source_hash,
                :requested_by_id, :authorization_id, :status, :attempt_count, :max_attempts,
                :available_at, :lease_token, :locked_until, :last_error_code,
                :created_at, :updated_at, :completed_at
            )
        """),
        values,
    )


async def _attempt(session: AsyncSession, ids: ReferenceIds, **overrides: Any) -> UUID:
    attempt_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO experience_generation_attempts (
                id, job_id, number, worker_id, lease_token, status, error_code,
                started_at, lease_expires_at, finished_at
            ) VALUES (
                :id, :job_id, :number, 'worker-a', :lease_token, :status, :error_code,
                :started_at, :lease_expires_at, :finished_at
            )
        """),
        {
            "id": attempt_id,
            "job_id": ids.job_id,
            "number": 1,
            "lease_token": uuid4(),
            "status": "running",
            "error_code": None,
            "started_at": BASE_TIME,
            "lease_expires_at": BASE_TIME + timedelta(seconds=30),
            "finished_at": None,
            **overrides,
        },
    )
    return attempt_id


async def _candidate(session: AsyncSession, ids: ReferenceIds, **overrides: Any) -> UUID:
    candidate_id = uuid4()
    await session.execute(
        text("""
            INSERT INTO experience_candidates (
                id, job_id, version, status, source_hash, content, content_hash, created_at
            ) VALUES (
                :id, :job_id, :version, :status, :source_hash,
                CAST(:content AS jsonb), :content_hash, :created_at
            )
        """),
        {
            "id": candidate_id,
            "job_id": ids.job_id,
            "version": 1,
            "status": "pending_review",
            "source_hash": "c" * 64,
            "content": "{}",
            "content_hash": "d" * 64,
            "created_at": BASE_TIME,
            **overrides,
        },
    )
    return candidate_id


async def test_0062_empty_tables_can_downgrade_and_upgrade_without_rewriting_references(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        before = await session.scalar(
            text("SELECT to_jsonb(r) FROM incident_reports r WHERE id = :id"),
            {"id": ids.report_id},
        )
    await asyncio.to_thread(command.downgrade, config(), "0061_trusted_operator_identity")
    try:
        async with database_session_factory() as session:
            for table in (
                "experience_generation_jobs",
                "experience_generation_attempts",
                "experience_candidates",
            ):
                assert (
                    await session.scalar(text("SELECT to_regclass(:table)"), {"table": table})
                    is None
                )
            assert (
                await session.scalar(
                    text("SELECT to_jsonb(r) FROM incident_reports r WHERE id = :id"),
                    {"id": ids.report_id},
                )
                == before
            )
    finally:
        await asyncio.to_thread(command.upgrade, config(), "head")
    await asyncio.to_thread(command.check, config())


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"policy_version": "unknown-policy"}, "ck_experience_jobs_policy"),
        ({"status": "unknown"}, "ck_experience_jobs_(status|completion)"),
        ({"max_attempts": 4}, "ck_experience_jobs_attempts"),
        ({"source_hash": "not-a-hash"}, "ck_experience_jobs_hash"),
        ({"source": json.dumps({"report_id": str(uuid4())})}, "ck_experience_jobs_source"),
        ({"source": "{}"}, "ck_experience_jobs_source"),
        ({"source": '{"report_id": null}'}, "ck_experience_jobs_source"),
        ({"source": "[]"}, "ck_experience_jobs_source"),
        ({"status": "running"}, "ck_experience_jobs_lease"),
        ({"completed_at": BASE_TIME}, "ck_experience_jobs_completion"),
        ({"available_at": BASE_TIME - timedelta(seconds=1)}, "ck_experience_jobs_time"),
    ],
)
async def test_0062_job_constraints_reject_invalid_rows(
    database_session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        with pytest.raises(DBAPIError, match=constraint):
            async with session.begin_nested():
                await _job(session, ids, **overrides)


async def test_0062_job_report_policy_is_unique(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        await _job(session, ids)
        with pytest.raises(DBAPIError, match="uq_experience_jobs_report_policy"):
            async with session.begin_nested():
                await _job(session, ids, id=uuid4())


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"number": 4}, "ck_experience_attempts_number"),
        ({"status": "failed"}, "ck_experience_attempts_completion"),
        ({"lease_expires_at": BASE_TIME}, "ck_experience_attempts_time"),
    ],
)
async def test_0062_attempt_constraints_reject_invalid_rows(
    database_session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        await _job(session, ids)
        with pytest.raises(DBAPIError, match=constraint):
            async with session.begin_nested():
                await _attempt(session, ids, **overrides)


@pytest.mark.parametrize(
    ("overrides", "constraint"),
    [
        ({"status": "active"}, "ck_experience_candidates_version_status"),
        ({"content_hash": "bad-hash"}, "ck_experience_candidates_hashes"),
        ({"content": "[]"}, "ck_experience_candidates_content"),
    ],
)
async def test_0062_candidate_constraints_reject_invalid_rows(
    database_session_factory: async_sessionmaker[AsyncSession],
    overrides: dict[str, Any],
    constraint: str,
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        await _job(session, ids)
        with pytest.raises(DBAPIError, match=constraint):
            async with session.begin_nested():
                await _candidate(session, ids, **overrides)


async def test_0062_job_preserves_source_identity_and_request_but_allows_lease_progress(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    protected_changes = (
        "source = source || '{\"changed\": true}'::jsonb",
        "source_hash = repeat('e', 64)",
        "requested_by_id = 'another-engineer'",
        "policy_version = 'changed-policy'",
        "authorization_id = :other_id",
        "report_id = :other_id",
        "max_attempts = 2",
        "created_at = created_at + interval '1 second'",
    )
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        await _job(session, ids)
        for change in protected_changes:
            with pytest.raises(DBAPIError, match="来源身份策略和请求不可变"):
                async with session.begin_nested():
                    await session.execute(
                        text(f"UPDATE experience_generation_jobs SET {change} WHERE id = :id"),
                        {"id": ids.job_id, "other_id": uuid4()},
                    )
        await session.execute(
            text("""
                UPDATE experience_generation_jobs SET status = 'running', attempt_count = 1,
                    lease_token = :token, locked_until = :until, updated_at = :at WHERE id = :id
            """),
            {
                "id": ids.job_id,
                "token": uuid4(),
                "until": BASE_TIME + timedelta(seconds=30),
                "at": BASE_TIME + timedelta(seconds=1),
            },
        )
        assert (
            await session.scalar(
                text("SELECT status FROM experience_generation_jobs WHERE id = :id"),
                {"id": ids.job_id},
            )
            == "running"
        )
        with pytest.raises(DBAPIError, match="不能删除已持久化的经验生成任务"):
            async with session.begin_nested():
                await session.execute(
                    text("DELETE FROM experience_generation_jobs WHERE id = :id"),
                    {"id": ids.job_id},
                )


@pytest.mark.parametrize("status", ["succeeded", "failed"])
async def test_0062_terminal_job_cannot_be_changed(
    database_session_factory: async_sessionmaker[AsyncSession], status: str
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        await _job(session, ids)
        await session.execute(
            text("""
                UPDATE experience_generation_jobs SET status = :status, completed_at = :at,
                    last_error_code = :error WHERE id = :id
            """),
            {
                "id": ids.job_id,
                "status": status,
                "at": BASE_TIME,
                "error": "generation_failed" if status == "failed" else None,
            },
        )
        with pytest.raises(DBAPIError, match="不能修改已终态的经验生成任务"):
            async with session.begin_nested():
                await session.execute(
                    text("UPDATE experience_generation_jobs SET updated_at = :at WHERE id = :id"),
                    {"id": ids.job_id, "at": BASE_TIME + timedelta(seconds=1)},
                )


@pytest.mark.parametrize("terminal_status", ["succeeded", "failed", "lease_expired"])
async def test_0062_attempt_identity_and_lease_are_immutable_and_terminal_fact_is_frozen(
    database_session_factory: async_sessionmaker[AsyncSession], terminal_status: str
) -> None:
    protected_changes = (
        "worker_id = 'worker-b'",
        "lease_token = :other_id",
        "job_id = :other_id",
        "number = 2",
        "started_at = started_at + interval '1 second'",
        "lease_expires_at = lease_expires_at + interval '1 second'",
    )
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        await _job(session, ids)
        attempt_id = await _attempt(session, ids)
        for change in protected_changes:
            with pytest.raises(DBAPIError, match="身份与租约不可变"):
                async with session.begin_nested():
                    await session.execute(
                        text(f"UPDATE experience_generation_attempts SET {change} WHERE id = :id"),
                        {"id": attempt_id, "other_id": uuid4()},
                    )
        with pytest.raises(DBAPIError, match="不能删除已持久化的经验生成尝试"):
            async with session.begin_nested():
                await session.execute(
                    text("DELETE FROM experience_generation_attempts WHERE id = :id"),
                    {"id": attempt_id},
                )
        await session.execute(
            text("""
                UPDATE experience_generation_attempts SET status = :status,
                    error_code = :error, finished_at = :at WHERE id = :id
            """),
            {
                "id": attempt_id,
                "status": terminal_status,
                "error": None if terminal_status == "succeeded" else "generation_failed",
                "at": BASE_TIME + timedelta(seconds=1),
            },
        )
        with pytest.raises(DBAPIError, match="不能修改已终态的经验生成尝试"):
            async with session.begin_nested():
                await session.execute(
                    text(
                        "UPDATE experience_generation_attempts SET finished_at = :at WHERE id = :id"
                    ),
                    {"id": attempt_id, "at": BASE_TIME + timedelta(seconds=2)},
                )


async def test_0062_candidate_is_append_only_and_one_per_job(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        await _job(session, ids)
        candidate_id = await _candidate(session, ids)
        for statement in (
            "UPDATE experience_candidates SET content = '{}'::jsonb WHERE id = :id",
            "DELETE FROM experience_candidates WHERE id = :id",
        ):
            with pytest.raises(DBAPIError, match="经验候选版本不可修改或删除"):
                async with session.begin_nested():
                    await session.execute(text(statement), {"id": candidate_id})
        with pytest.raises(DBAPIError, match="experience_candidates_job_id_key"):
            async with session.begin_nested():
                await _candidate(session, ids)


async def test_0062_existing_facts_block_downgrade_and_remain_queryable(
    database_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with database_session_factory() as session, session.begin():
        ids = await _references(session)
        await _job(session, ids)
        attempt_id = await _attempt(session, ids)
        candidate_id = await _candidate(session, ids)
    with pytest.raises(DBAPIError, match="不能删除已持久化的经验生成或候选事实"):
        await asyncio.to_thread(command.downgrade, config(), "0061_trusted_operator_identity")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0064_experience_lifecycle"
        )
        for table, record_id in (
            ("experience_generation_jobs", ids.job_id),
            ("experience_generation_attempts", attempt_id),
            ("experience_candidates", candidate_id),
        ):
            assert (
                await session.scalar(
                    text(f"SELECT id FROM {table} WHERE id = :id"), {"id": record_id}
                )
                == record_id
            )
