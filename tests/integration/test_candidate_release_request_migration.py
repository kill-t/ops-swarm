"""隔离PostgreSQL验证发布执行准入的可信正文、观测及原子请求事实。"""

import asyncio
import copy
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.approval.conditions.contracts import DecisionRecord
from opsswarm.execution.release.contracts import ReleaseObservation
from opsswarm.execution.release.policy import build_content
from opsswarm.fault_lab.release.contracts import ReleaseState
from opsswarm.planning.candidate.contracts import PlanRecord
from tests.api import test_condition_approval_api as approvals
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.integration import test_candidate_approval_migration as old
from tests.task_facts_helpers import BASE_TIME

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
encoded, digest = old.encoded, old.digest
ROUTE = (
    "/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}"
    "/execution-conditions/versions/{version}/release-execution-request"
)


def refusal():
    body = dict(
        approval_decision_id=str(uuid4()),
        approval_content_sha256="a" * 64,
        expected_incident_version=4,
        idempotency_key="native-release-request-124",
        reason="固定核验批准与只读发布版本，不执行写入",
    )
    return dict(
        run=uuid4(),
        job=uuid4(),
        version=1,
        subject="native-executor",
        authorization=uuid4(),
        body=body,
        raw=encoded(body),
        now=BASE_TIME,
        request=None,
    )


async def authorize(session, values, *, invalid=None):
    resources = dict(
        run_id=str(values["run"]), job_id=str(values["job"]), version=str(values["version"])
    )
    if invalid in resources:
        resources[invalid] = "2" if invalid == "version" else str(uuid4())
    await session.execute(
        text(
            "INSERT INTO operator_authorizations (id,policy_version,subject_id,credential_id,role,"
            "permissions,operation,method,route,resources,body_sha256,authorized_at) VALUES "
            "(:id,'operator-auth-v1',:subject,'native',:role,"
            "CAST(:permissions AS jsonb),:operation,"
            "'POST',:route,CAST(:resources AS jsonb),:hash,:now)"
        ),
        dict(
            id=values["authorization"],
            subject=values["subject"],
            role="engineer" if invalid == "role" else "executor",
            permissions=encoded([] if invalid == "permission" else ["execute_action"]),
            operation="check_candidate_execution"
            if invalid == "operation"
            else "prepare_candidate_release_execution",
            route=ROUTE + ("/commands" if invalid == "route" else ""),
            resources=encoded(resources),
            hash="f" * 64 if invalid == "body_hash" else digest(values["raw"]),
            now=values["now"],
        ),
    )


async def insert_audit(session, values, *, outcome="error"):
    await session.execute(
        text(
            "INSERT INTO candidate_release_execution_commands "
            "(authorization_id,investigation_run_id,"
            "candidate_job_id,verification_version,subject_id,command,raw_body,command_sha256,"
            "outcome,http_status,request_id,error_code,error_message,created_at) VALUES "
            "(:authorization,:run,:job,:version,:subject,CAST(:body AS jsonb),:raw,:hash,"
            ":outcome,:status,:request,:error,:message,:now)"
        ),
        {
            **values,
            "body": encoded(values["body"]),
            "hash": digest(encoded(values["body"])),
            "outcome": outcome,
            "status": {"applied": 201, "replayed": 200, "error": 409}[outcome],
            "request": None if outcome == "error" else values["request"],
            "error": "candidate_release_conflict" if outcome == "error" else None,
            "message": "候选发布准备被拒绝" if outcome == "error" else None,
        },
    )


async def apply(factory, values, *, audit=True, nested=False):
    async with factory() as session, session.begin():
        if nested:
            async with session.begin_nested():
                await _insert_fact(session, values, audit=audit)
        else:
            await _insert_fact(session, values, audit=audit)


async def _insert_fact(session, values, *, audit):
    await authorize(session, values)
    await session.execute(
        text(
            "INSERT INTO candidate_release_execution_requests SELECT * FROM "
            "jsonb_populate_record(NULL::candidate_release_execution_requests,CAST(:fact AS jsonb))"
        ),
        {"fact": encoded(values["fact"])},
    )
    if audit:
        await insert_audit(session, values, outcome="applied")


async def ready(c, *, kind="terminal_handoff"):
    await approvals.ready(c, kind=kind)
    request = (await approvals.create_request(c)).json()["request"]["record"]
    response = await approvals.decide(c, approvals.decision_body(c, request))
    assert response.status_code == 201, response.text
    approval = DecisionRecord.model_validate(response.json()["decision"]["record"])
    observation = ReleaseObservation(
        state=ReleaseState(
            version="2026.01.01-rc1",
            revision=2,
            command_count=1,
            release_500_enabled=True,
            updated_at=c.clock(),
        ),
        observed_at=c.clock(),
    )
    operation = uuid5(approval.id, "candidate-release-operation:v1")
    content = build_content(
        approval, PlanRecord.model_validate(c.plan_record), observation, operation
    )
    body = dict(
        approval_decision_id=str(approval.id),
        approval_content_sha256=approval.content_sha256,
        expected_incident_version=approval.incident_version,
        idempotency_key="native-release-request-124",
        reason="保存可信准备事实，不派发",
    )
    identifier = uuid5(approval.id, "candidate-release-request:v1")
    authorization = uuid4()
    fact = dict(
        id=str(identifier),
        operation_id=str(operation),
        investigation_run_id=str(approval.investigation_run_id),
        candidate_job_id=str(approval.candidate_job_id),
        approval_decision_id=str(approval.id),
        approval_content_sha256=approval.content_sha256,
        verification_version=approval.verification_version,
        incident_version=approval.incident_version,
        status="prepared",
        policy_version="candidate-release-request-v1",
        requested_by_id="native-executor",
        authorization_id=str(authorization),
        idempotency_key=body["idempotency_key"],
        command_sha256=digest(encoded(body)),
        content=content.model_dump(mode="json"),
        content_sha256=digest(encoded(content.model_dump(mode="json"))),
        created_at=c.clock().isoformat(),
        expires_at=min(
            c.clock() + timedelta(seconds=30),
            c.clock().fromisoformat(c.verification_record["expires_at"]),
        ).isoformat(),
    )
    return dict(
        run=approval.investigation_run_id,
        job=approval.candidate_job_id,
        version=approval.verification_version,
        subject="native-executor",
        authorization=authorization,
        body=body,
        raw=encoded(body),
        now=c.clock(),
        request=identifier,
        fact=fact,
    )


async def test_0080_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, old.config())
    try:
        await asyncio.to_thread(command.downgrade, old.config(), "0079_lab_release_state")
        async with database_session_factory() as session:
            assert (
                await session.scalar(
                    text("SELECT to_regclass('candidate_release_execution_requests')")
                )
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
async def test_0080_refusal_requires_precise_admission(database_session_factory, invalid):
    with pytest.raises(DBAPIError, match="同源可信准入"):
        async with database_session_factory() as session, session.begin():
            values = refusal()
            await authorize(session, values, invalid=invalid)
            await insert_audit(session, values)


async def test_0080_raw_contract_error_immutability_and_drop_protection(database_session_factory):
    original = refusal()
    for change in (
        {"expected_incident_version": True},
        {"expected_incident_version": 4.0},
        {"expected_incident_version": "4"},
        {"actor_id": "forged"},
        {"dispatch_eligible": True},
        {"approval_content_sha256": "bad"},
        {"reason": "替换正文"},
    ):
        values = {
            **original,
            "authorization": uuid4(),
            "raw": encoded({**original["body"], **change}),
        }
        with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
            async with database_session_factory() as session, session.begin():
                await authorize(session, values)
                await insert_audit(session, values)
    async with database_session_factory() as session, session.begin():
        await authorize(session, original)
        await insert_audit(session, original)
    for sql in (
        "UPDATE candidate_release_execution_commands SET error_message='改写'",
        "DELETE FROM candidate_release_execution_commands",
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with database_session_factory() as session, session.begin():
                await session.execute(text(sql))
    with pytest.raises(Exception, match="不能删除已保存"):
        await asyncio.to_thread(command.downgrade, old.config(), "0079_lab_release_state")


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_0080_native_content_atomicity_and_history(wait_context, kind):
    c = wait_context
    values = await ready(c, kind=kind)
    for change in (
        {"approval_content_sha256": "f" * 64},
        {"verification_version": 2},
        {"incident_version": 99},
        {"content_sha256": "e" * 64},
        {"expires_at": (c.clock() + timedelta(seconds=31)).isoformat()},
    ):
        with pytest.raises(DBAPIError):
            await apply(c.db, {**values, "fact": {**values["fact"], **change}})
    for mutation in ("dispatch", "revision", "path", "simulation", "version", "step"):
        fact = copy.deepcopy(values["fact"])
        if mutation == "dispatch":
            fact["content"]["dispatch_eligible"] = 0
        elif mutation == "revision":
            fact["content"]["cas_command"]["expected_revision"] = 3
        elif mutation == "path":
            fact["content"]["observation"]["source_path"] = "/__admin/faults/release-500"
        elif mutation == "simulation":
            fact["content"]["observation"]["state"]["simulation"] = 1
        elif mutation == "version":
            fact["content"]["observation"]["state"]["version"] = "stable"
        else:
            fact["content"]["step"]["parameters"]["target_version"] = "other-version"
        fact["content_sha256"] = digest(encoded(fact["content"]))
        with pytest.raises(DBAPIError):
            await apply(c.db, {**values, "fact": fact})
    with pytest.raises(DBAPIError, match="同事务精确成功命令闭环"):
        await apply(c.db, values, audit=False)
    await approvals.occupy_budget(c)
    with pytest.raises(DBAPIError):
        await apply(c.db, values)
    async with c.db() as session, session.begin():
        await session.execute(text("SET LOCAL session_replication_role=replica"))
        await session.execute(
            text(
                "DELETE FROM investigation_call_reservations WHERE investigation_run_id=:run "
                "AND status='unknown' AND operation='synthesize' "
                "AND invocation_index BETWEEN 40 AND 59"
            ),
            {"run": values["run"]},
        )
        await session.execute(text("SET LOCAL session_replication_role=origin"))
    await apply(c.db, values, nested=True)
    # 双来源成功事实经过应用完整历史读取，避免仅SQL侧自洽。
    from opsswarm.execution.release.store import CandidateReleaseRequestStore

    view = await CandidateReleaseRequestStore(c.db, clock=c.clock).get(
        values["run"], values["job"], values["version"]
    )
    assert view.record.id == values["request"] and view.availability == "current"
    c.clock.current += timedelta(seconds=30)
    async with c.db() as holder, holder.begin():
        await holder.execute(
            text("SELECT id FROM operator_authorizations WHERE id=:id FOR UPDATE"),
            {"id": values["authorization"]},
        )
        replay = {**values, "authorization": uuid4(), "now": c.clock()}
        async with c.db() as session, session.begin():
            await session.execute(text("SET LOCAL lock_timeout='1s'"))
            await authorize(session, replay)
            await insert_audit(session, replay, outcome="replayed")
    expired = await CandidateReleaseRequestStore(c.db, clock=c.clock).get(
        values["run"], values["job"], values["version"]
    )
    assert expired.availability == "expired"
    with pytest.raises(DBAPIError, match="不能降级"):
        async with c.db() as session, session.begin():
            values = {**values, "authorization": uuid4(), "now": c.clock()}
            await authorize(session, values)
            await insert_audit(session, values)
    for table in ("candidate_release_execution_requests", "candidate_release_execution_commands"):
        for sql in (
            f"UPDATE {table} SET created_at=created_at+interval '1 second'",
            f"DELETE FROM {table}",
        ):
            with pytest.raises(DBAPIError, match="不可更新或删除"):
                async with c.db() as session, session.begin():
                    await session.execute(text(sql))
    with pytest.raises(Exception, match="不能删除已保存"):
        await asyncio.to_thread(command.downgrade, old.config(), "0079_lab_release_state")
    await approvals.append_verification(c)
    async with c.db() as session, session.begin():
        await session.execute(text("SET LOCAL session_replication_role=replica"))
        await session.execute(
            text("UPDATE candidate_condition_verifications SET content_sha256=:hash WHERE id=:id"),
            {"hash": "f" * 64, "id": UUID(c.verification_record["id"])},
        )
    for outcome in ("replayed", "error"):
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                damaged = {**values, "authorization": uuid4(), "now": c.clock()}
                if outcome == "error":
                    damaged["body"] = {**damaged["body"], "idempotency_key": "damaged-new-key-124"}
                    damaged["raw"] = encoded(damaged["body"])
                await authorize(session, damaged)
                await insert_audit(session, damaged, outcome=outcome)
