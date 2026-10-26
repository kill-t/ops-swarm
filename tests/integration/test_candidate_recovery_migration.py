"""真实PostgreSQL核查恢复验证迁移、完整执行来源和不可变终态。"""

import asyncio
from datetime import timedelta, timezone
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from tests.api.test_candidate_recovery_api import get, prepare
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.api.test_release_execution_jobs_api import (
    current_fixture_time as current_fixture_time,
)
from tests.integration import test_candidate_approval_migration as old
from tests.integration.test_release_execution_migration import create_native_job

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
encoded, digest = old.encoded, old.digest


async def test_0083_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, old.config())
    try:
        await asyncio.to_thread(command.downgrade, old.config(), "0082_candidate_release_job")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT to_regclass('candidate_recovery_jobs')")) is None
            )
            assert (
                await session.scalar(
                    text("SELECT to_regprocedure('candidate_recovery_utc_text(timestamptz)')")
                )
                is None
            )
        await asyncio.to_thread(command.upgrade, old.config(), "head")
        await asyncio.to_thread(command.check, old.config())
    finally:
        await asyncio.to_thread(command.upgrade, old.config(), "head")


async def test_0083_isolated_job_cannot_replace_full_execution_history(database_session_factory):
    with pytest.raises(DBAPIError, match="终态发布执行来源"):
        async with database_session_factory() as session, session.begin():
            await session.execute(
                text(
                    "INSERT INTO candidate_recovery_jobs "
                    "(id,investigation_run_id,candidate_job_id,verification_version,operation_id,"
                    "execution_result_sha256,policy_version,window_start,window_end,status,"
                    "attempt_count,created_at,updated_at) VALUES "
                    "(:id,:run,:candidate,1,:operation,:hash,'candidate-recovery-v1',"
                    "clock_timestamp(),clock_timestamp(),'queued',0,now(),now())"
                ),
                dict(id=uuid4(), run=uuid4(), candidate=uuid4(), operation=uuid4(), hash="a" * 64),
            )


async def completed_blocked_execution(c):
    """使用124可信来源与127完整结算，恢复验证无需伪造父执行事实。"""
    job = await create_native_job(c)
    identifier, token = uuid4(), uuid4()
    async with c.db() as session, session.begin():
        now = await session.scalar(text("SELECT clock_timestamp()"))
        values = dict(job=job["id"], attempt=identifier, token=token, now=now)
        await session.execute(
            text(
                "UPDATE candidate_release_execution_jobs SET status='running',attempt_count=1,"
                "locked_by='native-worker',lease_token=:token,locked_until=:expires,"
                "updated_at=:now WHERE id=:job"
            ),
            {**values, "expires": now + timedelta(seconds=60)},
        )
        await session.execute(
            text(
                "INSERT INTO candidate_release_execution_attempts "
                "(id,job_id,number,lease_token,worker_id,status,started_at,lease_expires_at) "
                "VALUES (:attempt,:job,1,:token,'native-worker','running',:now,:expires)"
            ),
            {**values, "expires": now + timedelta(seconds=60)},
        )
        await session.execute(
            text(
                "UPDATE candidate_release_execution_attempts SET status='failed',"
                "finished_at=:now,error_code='release_dispatch_expired' WHERE id=:attempt"
            ),
            values,
        )
        await session.execute(
            text(
                "UPDATE candidate_release_execution_jobs SET status='failed',locked_by=NULL,"
                "lease_token=NULL,locked_until=NULL,completed_at=:now,updated_at=:now,"
                "last_error_code='release_dispatch_expired' WHERE id=:job"
            ),
            values,
        )
        result = dict(
            version="candidate-release-execution-result-v1",
            request_id=job["request_id"],
            request_content_sha256=job["request_content_sha256"],
            operation_id=job["operation_id"],
            dispatch_sha256=job["dispatch_sha256"],
            outcome="blocked",
            observation=None,
            reason_code="release_dispatch_expired",
            created_at=now.isoformat(),
            parent_advanced=False,
        )
        await session.execute(
            text(
                "INSERT INTO candidate_release_execution_results "
                "(job_id,attempt_id,content,content_sha256,created_at) "
                "VALUES (:job,:attempt,CAST(:content AS jsonb),:hash,:now)"
            ),
            {**values, "content": encoded(result), "hash": digest(encoded(result))},
        )
    return job


async def save_recovery(c, job, *, invalid=None):
    async with c.db() as session, session.begin():
        # 同一数据库时刻也避免INSERT中的两个clock_timestamp产生不同微秒。
        await session.execute(
            text(
                "INSERT INTO candidate_recovery_jobs "
                "(id,investigation_run_id,candidate_job_id,verification_version,operation_id,"
                "execution_result_sha256,policy_version,window_start,window_end,status,"
                "attempt_count,created_at,updated_at) SELECT "
                "j.id,j.investigation_run_id,j.candidate_job_id,j.verification_version,"
                "j.operation_id,r.content_sha256,'candidate-recovery-v1',j.completed_at,"
                "least(j.completed_at+interval '120 seconds',run.started_at+"
                "(run.call_budget->>'max_duration_seconds')::integer*interval '1 second'),"
                "'queued',0,now(),now() FROM candidate_release_execution_jobs j "
                "JOIN candidate_release_execution_results r ON r.job_id=j.id "
                "JOIN investigation_runs run ON run.id=j.investigation_run_id WHERE j.id=:job"
            ),
            {"job": job["id"]},
        )
        now = await session.scalar(text("SELECT clock_timestamp()"))
        if invalid == "precision":
            # 零小数与省略小数是同一时刻，只允许Pydantic的省略形式。
            now = now.replace(microsecond=0) + timedelta(seconds=1)
        await session.execute(
            text(
                "UPDATE candidate_recovery_jobs SET status='completed',completed_at=:now,"
                "updated_at=:now WHERE id=:job"
            ),
            {"job": job["id"], "now": now},
        )
        source_hash = await session.scalar(
            text("SELECT execution_result_sha256 FROM candidate_recovery_jobs WHERE id=:job"),
            {"job": job["id"]},
        )
        content = dict(
            version="candidate-recovery-result-v1",
            execution_job_id=job["id"],
            execution_result_sha256=source_hash,
            operation_id=job["operation_id"],
            outcome="not_executed",
            attempt_id=None,
            reason_code="execution_not_applied",
            created_at=now.isoformat().replace("+00:00", "Z"),
            parent_advanced=False,
        )
        if invalid == "reason":
            content["reason_code"] = "probe_source_failed"
        elif invalid == "utc":
            content["created_at"] = now.astimezone(timezone(timedelta(hours=1))).isoformat()
        elif invalid == "utc_zero":
            content["created_at"] = now.isoformat()
        elif invalid == "precision":
            content["created_at"] = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
        await session.execute(
            text(
                "INSERT INTO candidate_recovery_results (job_id,content,content_sha256,created_at)"
                " VALUES (:job,CAST(:content AS jsonb),:hash,:now)"
            ),
            dict(job=job["id"], content=encoded(content), hash=digest(encoded(content)), now=now),
        )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0083_full_source_terminal_and_materials_are_immutable(wait_context):
    c = wait_context
    job = await completed_blocked_execution(c)
    # 正确摘要不能放行异体；独立事务核查拒绝后任务和结果均完整回滚。
    for invalid in ("reason", "utc", "utc_zero", "precision"):
        with pytest.raises(DBAPIError, match="未适用执行|不可变终态"):
            await save_recovery(c, job, invalid=invalid)
        async with c.db() as session:
            assert (
                await session.scalar(text("SELECT count(*) FROM candidate_recovery_results")) == 0
            )
            assert await session.scalar(text("SELECT count(*) FROM candidate_recovery_jobs")) == 0
    await save_recovery(c, job)
    for statement in (
        "UPDATE candidate_recovery_jobs SET operation_id=gen_random_uuid()",
        "UPDATE candidate_recovery_jobs SET status='queued',completed_at=NULL",
        "DELETE FROM candidate_recovery_jobs",
        "UPDATE candidate_recovery_results SET content_sha256=repeat('f',64)",
        "DELETE FROM candidate_recovery_results",
    ):
        with pytest.raises(DBAPIError, match="不可|不一致"):
            async with c.db() as session, session.begin():
                await session.execute(text(statement))
    with pytest.raises(Exception, match="不能删除已保存的候选恢复验证事实"):
        await asyncio.to_thread(command.downgrade, old.config(), "0082_candidate_release_job")


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0083_attempt_uuid_alias_cannot_create_unreadable_result(wait_context):
    c = wait_context
    await prepare(c)
    claim = await c.recovery.claim_next(worker_id="native-canonical-result")
    assert claim is not None
    await c.recovery.save_sample(claim, 1, error_code="probe_unavailable")
    view = (await get(c)).json()
    for alias in (claim.attempt_id.urn, claim.attempt_id.hex):
        with pytest.raises(DBAPIError, match="最后同源终态尝试"):
            async with c.db() as session, session.begin():
                now = await session.scalar(text("SELECT clock_timestamp()"))
                await session.execute(
                    text(
                        "UPDATE candidate_recovery_attempts SET status='failed',finished_at=:now "
                        "WHERE id=:attempt"
                    ),
                    dict(now=now, attempt=claim.attempt_id),
                )
                await session.execute(
                    text(
                        "UPDATE candidate_recovery_jobs SET status='completed',completed_at=:now,"
                        "updated_at=:now,locked_by=NULL,lease_token=NULL,locked_until=NULL "
                        "WHERE id=:job"
                    ),
                    dict(now=now, job=claim.job_id),
                )
                content = dict(
                    version="candidate-recovery-result-v1",
                    execution_job_id=str(claim.job_id),
                    execution_result_sha256=view["record"]["execution_result_sha256"],
                    operation_id=view["record"]["operation_id"],
                    outcome="source_failed",
                    attempt_id=alias,
                    reason_code="probe_source_failed",
                    created_at=now.isoformat().replace("+00:00", "Z"),
                    parent_advanced=False,
                )
                await session.execute(
                    text(
                        "INSERT INTO candidate_recovery_results "
                        "(job_id,content,content_sha256,created_at) "
                        "VALUES (:job,CAST(:content AS jsonb),:hash,:now)"
                    ),
                    dict(
                        job=claim.job_id,
                        content=encoded(content),
                        hash=digest(encoded(content)),
                        now=now,
                    ),
                )
        assert (await get(c)).json() == view
    await c.recovery.complete(claim)
    assert (await get(c)).json()["result"]["reason_code"] == "probe_source_failed"
