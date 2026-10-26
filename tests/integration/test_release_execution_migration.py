"""真实PostgreSQL核查执行任务原生准入、不可变审计与空库迁移。"""

import asyncio
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.integration import test_candidate_approval_migration as old
from tests.integration import test_candidate_release_request_migration as requests
from tests.task_facts_helpers import BASE_TIME

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
encoded, digest = old.encoded, old.digest
ROUTE = (
    "/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}"
    "/execution-conditions/versions/{version}/release-execution-request/execution-job"
)


def refusal():
    body = dict(
        request_id=str(uuid4()),
        request_content_sha256="a" * 64,
        expected_incident_version=4,
        idempotency_key="native-release-job-127",
        reason="保留可信失败命令，不能伪造成功任务",
    )
    return dict(
        authorization=uuid4(),
        run=uuid4(),
        job=uuid4(),
        version=1,
        subject="native-executor",
        body=body,
        raw=encoded(body),
        now=BASE_TIME,
    )


async def authorize(session, values, *, invalid=None):
    resources = dict(
        run_id=str(values["run"]), job_id=str(values["job"]), version=str(values["version"])
    )
    if invalid in resources:
        resources[invalid] = "2" if invalid == "version" else str(uuid4())
    await session.execute(
        text(
            "INSERT INTO operator_authorizations (id,policy_version,subject_id,credential_id,"
            "role,permissions,operation,method,route,resources,body_sha256,authorized_at) "
            "VALUES (:authorization,'operator-auth-v1',:subject,'native',:role,"
            "CAST(:permissions AS jsonb),:operation,'POST',:route,CAST(:resources AS jsonb),"
            ":hash,:now)"
        ),
        {
            **values,
            "role": "engineer" if invalid == "role" else "executor",
            "permissions": encoded([] if invalid == "permission" else ["execute_action"]),
            "operation": "prepare_candidate_release_execution"
            if invalid == "operation"
            else "create_candidate_release_execution_job",
            "route": ROUTE + ("/forged" if invalid == "route" else ""),
            "resources": encoded(resources),
            "hash": "f" * 64 if invalid == "body_hash" else digest(values["raw"]),
        },
    )


async def insert_audit(session, values, *, outcome="error"):
    await session.execute(
        text(
            "INSERT INTO candidate_release_execution_job_commands "
            "(authorization_id,investigation_run_id,candidate_job_id,verification_version,"
            "subject_id,command,raw_body,command_sha256,outcome,http_status,execution_job_id,"
            "error_code,error_message,created_at) VALUES "
            "(:authorization,:run,:job,:version,:subject,CAST(:body AS jsonb),:raw,:hash,"
            ":outcome,:status,:execution_job,:error,:message,:now)"
        ),
        {
            **values,
            "body": encoded(values["body"]),
            "hash": digest(encoded(values["body"])),
            "outcome": outcome,
            "status": {"error": 409, "applied": 201, "replayed": 200}[outcome],
            "execution_job": None if outcome == "error" else values.get("execution_job", uuid4()),
            "error": "release_execution_conflict" if outcome == "error" else None,
            "message": "发布执行任务被拒绝" if outcome == "error" else None,
        },
    )


async def test_0082_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, old.config())
    try:
        await asyncio.to_thread(command.downgrade, old.config(), "0081_lab_release_dispatch")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT to_regclass('candidate_release_execution_jobs')"))
                is None
            )
        await asyncio.to_thread(command.upgrade, old.config(), "head")
        await asyncio.to_thread(command.check, old.config())
    finally:
        await asyncio.to_thread(command.upgrade, old.config(), "head")


@pytest.mark.parametrize(
    "invalid",
    ["role", "permission", "operation", "route", "run_id", "job_id", "version", "body_hash"],
)
async def test_0082_error_requires_precise_admission(database_session_factory, invalid):
    with pytest.raises(DBAPIError, match="同源可信准入"):
        async with database_session_factory() as session, session.begin():
            values = refusal()
            await authorize(session, values, invalid=invalid)
            await insert_audit(session, values)


@pytest.mark.parametrize("outcome", ["applied", "replayed"])
async def test_0082_cannot_forge_success_without_job(database_session_factory, outcome):
    with pytest.raises(DBAPIError, match="同源成功事实"):
        async with database_session_factory() as session, session.begin():
            values = refusal()
            await authorize(session, values)
            await insert_audit(session, values, outcome=outcome)


async def test_0082_raw_contract_and_terminal_audit_are_immutable(database_session_factory):
    original = refusal()
    for mutation in (
        {"expected_incident_version": True},
        {"expected_incident_version": 4.0},
        {"expected_incident_version": "4"},
        {"credential_id": "forged"},
        {"request_content_sha256": "invalid"},
        {"parent_advanced": True},
        {"reason": "替换正文"},
    ):
        values = {
            **original,
            "authorization": uuid4(),
            "raw": encoded({**original["body"], **mutation}),
        }
        with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
            async with database_session_factory() as session, session.begin():
                await authorize(session, values)
                await insert_audit(session, values)
    async with database_session_factory() as session, session.begin():
        await authorize(session, original)
        await insert_audit(session, original)
    for statement in (
        "UPDATE candidate_release_execution_job_commands SET error_message='改写'",
        "UPDATE candidate_release_execution_job_commands SET outcome='applied',http_status=201",
        "DELETE FROM candidate_release_execution_job_commands",
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with database_session_factory() as session, session.begin():
                await session.execute(text(statement))
    with pytest.raises(Exception, match="不能删除已保存"):
        await asyncio.to_thread(command.downgrade, old.config(), "0081_lab_release_dispatch")


async def create_native_job(c):
    """使用既有完整124来源创建任务，避免仅伪造触发器可接受的孤立行。"""
    source = await requests.ready(c)
    await requests.apply(c.db, source)
    fact = source["fact"]
    body = dict(
        request_id=fact["id"],
        request_content_sha256=fact["content_sha256"],
        expected_incident_version=fact["incident_version"],
        idempotency_key="native-release-execution-127",
        reason="核查租约事务提交边界",
    )
    values = {
        **source,
        "authorization": uuid4(),
        "body": body,
        "raw": encoded(body),
        "execution_job": uuid4(),
    }
    dispatch = dict(
        operation_id=fact["operation_id"],
        command=fact["content"]["cas_command"],
        not_before=fact["created_at"],
        expires_at=fact["expires_at"],
    )
    job = dict(
        id=str(values["execution_job"]),
        investigation_run_id=fact["investigation_run_id"],
        candidate_job_id=fact["candidate_job_id"],
        verification_version=fact["verification_version"],
        request_id=fact["id"],
        request_content_sha256=fact["content_sha256"],
        operation_id=fact["operation_id"],
        policy_version="candidate-release-execution-v1",
        requested_by_id=values["subject"],
        credential_id="native",
        credential_binding_sha256="a" * 64,
        credential_expires_at=(c.clock() + timedelta(minutes=1)).isoformat(),
        authorization_id=str(values["authorization"]),
        idempotency_key=body["idempotency_key"],
        command_sha256=digest(encoded(body)),
        dispatch_command=dispatch,
        dispatch_sha256=digest(encoded(dispatch)),
        status="queued",
        phase="prepared",
        attempt_count=0,
        created_at=fact["created_at"],
        updated_at=fact["created_at"],
    )
    async with c.db() as session, session.begin():
        await authorize(session, values)
        await session.execute(
            text(
                "INSERT INTO candidate_release_execution_jobs SELECT * FROM "
                "jsonb_populate_record(NULL::candidate_release_execution_jobs,CAST(:fact AS jsonb))"
            ),
            {"fact": encoded(job)},
        )
        await insert_audit(session, values, outcome="applied")
    return job


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0082_terminal_transaction_crossing_lease_deadline_rolls_back(wait_context):
    c = wait_context
    job = await create_native_job(c)
    identifier, token = uuid4(), uuid4()
    async with c.db() as session, session.begin():
        now = await session.scalar(text("SELECT clock_timestamp()"))
        started = now - timedelta(seconds=55)
        expires = started + timedelta(seconds=60)
        await session.execute(
            text(
                "UPDATE candidate_release_execution_jobs SET status='running',attempt_count=1,"
                "locked_by='native-worker',lease_token=:token,locked_until=:expires,"
                "updated_at=:started "
                "WHERE id=:id"
            ),
            dict(id=job["id"], token=token, expires=expires, started=started),
        )
        await session.execute(
            text(
                "INSERT INTO candidate_release_execution_attempts "
                "(id,job_id,number,lease_token,worker_id,status,started_at,lease_expires_at) "
                "VALUES (:attempt,:job,1,:token,'native-worker','running',:started,:expires)"
            ),
            dict(attempt=identifier, job=job["id"], token=token, started=started, expires=expires),
        )
    with pytest.raises(DBAPIError, match="终态事务已越过当前尝试租约截止"):
        async with c.db() as session, session.begin():
            finished = await session.scalar(text("SELECT clock_timestamp()"))
            result = dict(
                version="candidate-release-execution-result-v1",
                request_id=job["request_id"],
                request_content_sha256=job["request_content_sha256"],
                operation_id=job["operation_id"],
                dispatch_sha256=job["dispatch_sha256"],
                outcome="blocked",
                observation=None,
                reason_code="release_dispatch_expired",
                created_at=finished.isoformat(),
                parent_advanced=False,
            )
            await session.execute(
                text(
                    "UPDATE candidate_release_execution_attempts SET status='failed',"
                    "finished_at=:now,"
                    "error_code='release_dispatch_expired' WHERE id=:id"
                ),
                dict(id=identifier, now=finished),
            )
            await session.execute(
                text(
                    "UPDATE candidate_release_execution_jobs SET status='failed',locked_by=NULL,"
                    "lease_token=NULL,locked_until=NULL,completed_at=:now,updated_at=:now,"
                    "last_error_code='release_dispatch_expired' WHERE id=:id"
                ),
                dict(id=job["id"], now=finished),
            )
            await session.execute(
                text(
                    "INSERT INTO candidate_release_execution_results "
                    "(job_id,attempt_id,content,content_sha256,created_at) "
                    "VALUES (:job,:attempt,CAST(:content AS jsonb),:hash,:now)"
                ),
                dict(
                    job=job["id"],
                    attempt=identifier,
                    content=encoded(result),
                    hash=digest(encoded(result)),
                    now=finished,
                ),
            )
            await session.execute(text("SELECT pg_sleep(5.1)"))
    async with c.db() as session:
        assert (
            await session.scalar(
                text("SELECT status FROM candidate_release_execution_jobs WHERE id=:id"),
                {"id": job["id"]},
            )
            == "running"
        )
        assert (
            await session.scalar(
                text("SELECT count(*) FROM candidate_release_execution_results WHERE job_id=:id"),
                {"id": job["id"]},
            )
            == 0
        )
