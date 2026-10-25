"""隔离PostgreSQL核验独立审批的原生准入、不可变与原子闭环。"""

import asyncio
from datetime import datetime
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from opsswarm.approval.conditions.authorization import BASE_ROUTE
from opsswarm.approval.conditions.contracts import (
    DecisionCommand,
    RequestCommand,
    RequestRecord,
)
from opsswarm.approval.conditions.policy import build_decision_content, build_request_content
from opsswarm.approval.conditions.queries import decision_id, request_id
from opsswarm.execution.conditions.contracts import ConditionRecord
from tests.api import test_condition_approval_api as api
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.integration import test_candidate_approval_migration as old
from tests.task_facts_helpers import BASE_TIME

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
encoded, digest = old.encoded, old.digest


def refusal(stage="request"):
    body = dict(
        expected_incident_version=3,
        idempotency_key="native-condition-approval",
        reason="原生准入核验",
    )
    if stage == "request":
        body.update(verification_id=str(uuid4()), verification_content_sha256="a" * 64)
    else:
        body.update(
            action="reject",
            request_id=str(uuid4()),
            request_content_sha256="b" * 64,
            acknowledge_verification=False,
        )
    return dict(
        stage=stage,
        run=uuid4(),
        job=uuid4(),
        version=1,
        subject="native-operator",
        authorization=uuid4(),
        body=body,
        raw=encoded(body),
        now=BASE_TIME,
        request=None,
        decision=None,
    )


async def authorize(session, values, *, invalid=None):
    stage = values["stage"]
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
            role="viewer"
            if invalid == "role"
            else "engineer"
            if stage == "request"
            else "approver",
            permissions=encoded(
                []
                if invalid == "permission"
                else ["request_approval" if stage == "request" else "decide_approval"]
            ),
            operation="forged_operation"
            if invalid == "operation"
            else "create_condition_approval_request"
            if stage == "request"
            else "decide_condition_approval",
            route=BASE_ROUTE
            + ("/decision" if stage == "decision" else "")
            + ("/commands" if invalid == "route" else ""),
            resources=encoded(resources),
            hash="c" * 64 if invalid == "body_hash" else digest(values["raw"]),
            now=values["now"],
        ),
    )


async def insert_audit(session, values, *, outcome="error"):
    await session.execute(
        text(
            "INSERT INTO condition_approval_commands (authorization_id,investigation_run_id,"
            "candidate_job_id,verification_version,stage,subject_id,command,raw_body,command_sha256,"
            "outcome,http_status,request_id,decision_id,error_code,error_message,created_at) "
            "VALUES "
            "(:authorization,:run,:job,:version,:stage,:subject,CAST(:body AS jsonb),:raw,:hash,"
            ":outcome,:status,:request,:decision,:error,:message,:now)"
        ),
        {
            **values,
            "body": encoded(values["body"]),
            "hash": digest(encoded(values["body"])),
            "outcome": outcome,
            "status": {"applied": 201, "replayed": 200, "error": 409}[outcome],
            "request": None if outcome == "error" else values["request"],
            "decision": None if outcome == "error" else values["decision"],
            "error": "condition_approval_conflict" if outcome == "error" else None,
            "message": "原生业务拒绝" if outcome == "error" else None,
        },
    )


async def apply(factory, values, *, audit=True):
    async with factory() as session, session.begin():
        await authorize(session, values)
        table = (
            "condition_approval_requests"
            if values["stage"] == "request"
            else "condition_approval_decisions"
        )
        await session.execute(
            text(
                f"INSERT INTO {table} SELECT * FROM "
                f"jsonb_populate_record(NULL::{table},CAST(:fact AS jsonb))"
            ),
            {"fact": encoded(values["fact"])},
        )
        if audit:
            await insert_audit(session, values, outcome="applied")


def request_values(c):
    verification = ConditionRecord.model_validate(c.verification_record)
    body = RequestCommand.model_validate(c.condition_request_body).model_dump(mode="json")
    content = build_request_content(verification).model_dump(mode="json")
    auth = uuid4()
    fact = dict(
        id=str(request_id(verification.id)),
        investigation_run_id=str(verification.investigation_run_id),
        candidate_job_id=str(verification.candidate_job_id),
        verification_id=str(verification.id),
        verification_content_sha256=verification.content_sha256,
        verification_version=verification.version,
        incident_version=verification.incident_version,
        policy_version="condition-approval-v1",
        status="pending",
        requested_by_id="native-operator",
        authorization_id=str(auth),
        idempotency_key=body["idempotency_key"],
        command_sha256=digest(encoded(body)),
        content=content,
        content_sha256=digest(encoded(content)),
        created_at=c.clock().isoformat(),
        expires_at=verification.expires_at.isoformat(),
    )
    return dict(
        stage="request",
        run=verification.investigation_run_id,
        job=verification.candidate_job_id,
        version=verification.version,
        subject="native-operator",
        authorization=auth,
        body=body,
        raw=encoded(body),
        now=c.clock(),
        request=UUID(fact["id"]),
        decision=None,
        fact=fact,
    )


def decision_values(c, request, *, action="approve"):
    record = RequestRecord.model_validate(request["fact"])
    body = DecisionCommand.model_validate(
        api.decision_body(
            c, request["fact"], action=action, key=f"native-{action}-{record.verification_version}"
        )
    ).model_dump(mode="json")
    content = build_decision_content(record, action).model_dump(mode="json")
    auth, identifier = uuid4(), decision_id(record.id)
    fact = dict(
        id=str(identifier),
        investigation_run_id=str(record.investigation_run_id),
        candidate_job_id=str(record.candidate_job_id),
        request_id=str(record.id),
        request_content_sha256=record.content_sha256,
        verification_id=str(record.verification_id),
        verification_content_sha256=record.verification_content_sha256,
        verification_version=record.verification_version,
        incident_version=record.incident_version,
        policy_version="condition-approval-v1",
        outcome=content["outcome"],
        decided_by_id="native-operator",
        authorization_id=str(auth),
        idempotency_key=body["idempotency_key"],
        command_sha256=digest(encoded(body)),
        content=content,
        content_sha256=digest(encoded(content)),
        created_at=c.clock().isoformat(),
    )
    return dict(
        stage="decision",
        run=record.investigation_run_id,
        job=record.candidate_job_id,
        version=record.verification_version,
        subject="native-operator",
        authorization=auth,
        body=body,
        raw=encoded(body),
        now=c.clock(),
        request=record.id,
        decision=identifier,
        fact=fact,
    )


async def test_0078_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, old.config())
    try:
        await asyncio.to_thread(command.downgrade, old.config(), "0077_candidate_conditions")
        async with database_session_factory() as session:
            assert (
                await session.scalar(text("SELECT to_regclass('condition_approval_requests')"))
                is None
            )
        await asyncio.to_thread(command.upgrade, old.config(), "head")
        await asyncio.to_thread(command.check, old.config())
    finally:
        await asyncio.to_thread(command.upgrade, old.config(), "head")


@pytest.mark.parametrize("stage", ["request", "decision"])
@pytest.mark.parametrize(
    "invalid",
    ["role", "permission", "operation", "route", "run_id", "job_id", "version", "body_hash"],
)
async def test_0078_precise_admission(database_session_factory, stage, invalid):
    values = refusal(stage)
    with pytest.raises(DBAPIError, match="同源可信准入"):
        async with database_session_factory() as session, session.begin():
            await authorize(session, values, invalid=invalid)
            await insert_audit(session, values)


async def test_0078_raw_contract_and_immutable_refusals(database_session_factory):
    for stage in ("request", "decision"):
        original = refusal(stage)
        for change in (
            {"expected_incident_version": True},
            {"expected_incident_version": "3"},
            {"expected_incident_version": 3.0},
            {"actor_id": "forged"},
            {"execution_eligible": True},
            {"reason": "  "},
            {"reason": "不同正文"},
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
        if stage == "decision":
            for ack in (True, 1, "false"):
                values = {
                    **original,
                    "authorization": uuid4(),
                    "body": {**original["body"], "acknowledge_verification": ack},
                }
                values["raw"] = encoded(values["body"])
                with pytest.raises(DBAPIError):
                    async with database_session_factory() as session, session.begin():
                        await authorize(session, values)
                        await insert_audit(session, values)
        async with database_session_factory() as session, session.begin():
            await authorize(session, original)
            await insert_audit(session, original)
    for sql in (
        "UPDATE condition_approval_commands SET error_message='篡改'",
        "DELETE FROM condition_approval_commands",
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with database_session_factory() as session, session.begin():
                await session.execute(text(sql))
    with pytest.raises(Exception, match="不能删除已保存"):
        await asyncio.to_thread(command.downgrade, old.config(), "0077_candidate_conditions")


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_0078_native_facts_atomic_history_and_replay(wait_context, kind):
    c = wait_context
    await api.ready(c, kind=kind)
    request = request_values(c)
    # 引用、期限、正文和来源必须同时绑定，不能先写孤儿事实再补审计。
    for change in (
        {"verification_content_sha256": "c" * 64},
        {"verification_version": 2},
        {"incident_version": 99},
        {"content_sha256": "d" * 64},
        {"expires_at": "2099-01-01T00:00:00Z"},
    ):
        with pytest.raises(DBAPIError):
            await apply(c.db, {**request, "fact": {**request["fact"], **change}})
    with pytest.raises(DBAPIError, match="同事务精确成功命令闭环"):
        await apply(c.db, request, audit=False)
    await apply(c.db, request)
    approval = decision_values(c, request)
    with pytest.raises(DBAPIError, match="同事务精确成功命令闭环"):
        await apply(c.db, approval, audit=False)
    await apply(c.db, approval)
    assert (await api.get(c)).json()["availability"] == "recorded"
    result = await api.get(c, "/decision")
    assert result.status_code == 200, result.text
    assert result.json()["availability"] == "current"
    for table in ("condition_approval_requests", "condition_approval_decisions"):
        for sql in (f"UPDATE {table} SET content='{{}}'::jsonb", f"DELETE FROM {table}"):
            with pytest.raises(DBAPIError, match="不可更新或删除"):
                async with c.db() as session, session.begin():
                    await session.execute(text(sql))
    await api.append_verification(c)
    # 旧授权锁不应阻塞历史重放；新请求/批准与新核验共享父锁。
    async with c.db() as holder, holder.begin():
        await holder.execute(
            text("SELECT id FROM operator_authorizations WHERE id=:id FOR UPDATE"),
            {"id": request["authorization"]},
        )
        for original in (request, approval):
            replay = {**original, "authorization": uuid4()}
            async with c.db() as session, session.begin():
                await session.execute(text("SET LOCAL lock_timeout='1s'"))
                await authorize(session, replay)
                await insert_audit(session, replay, outcome="replayed")
    for original in (request, approval):
        with pytest.raises(DBAPIError, match="不能降级"):
            async with c.db() as session, session.begin():
                downgrade = {**original, "authorization": uuid4()}
                await authorize(session, downgrade)
                await insert_audit(session, downgrade)
    # 损坏最高核验后，原生旧批准重放也不能接受旧前缀。
    async with c.db() as session, session.begin():
        await session.execute(text("SET LOCAL session_replication_role=replica"))
        await session.execute(
            text("UPDATE candidate_condition_verifications SET content_sha256=:hash WHERE id=:id"),
            {"hash": "e" * 64, "id": c.verification_record["id"]},
        )
    with pytest.raises(DBAPIError):
        async with c.db() as session, session.begin():
            replay = {**approval, "authorization": uuid4()}
            await authorize(session, replay)
            await insert_audit(session, replay, outcome="replayed")


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0078_native_current_and_negative_convergence(wait_context):
    c = wait_context
    await api.ready(c)
    first = request_values(c)
    await apply(c.db, first)
    await api.append_verification(c)
    with pytest.raises(DBAPIError, match="最新完整核验"):
        await apply(c.db, decision_values(c, first))
    with pytest.raises(DBAPIError, match="最新完整核验"):
        await apply(c.db, {**first, "authorization": uuid4()})
    second = request_values(c)
    await apply(c.db, second)
    await api.occupy_budget(c)
    with pytest.raises(DBAPIError):
        await apply(c.db, decision_values(c, second))
    await apply(c.db, decision_values(c, first, action="reject"))
    with pytest.raises(DBAPIError):
        await apply(c.db, decision_values(c, second, action="expire"))
    c.clock.current = datetime.fromisoformat(second["fact"]["expires_at"])
    await apply(c.db, decision_values(c, second, action="expire"))
    await c.app.state.condition_approval_store.get_decision(second["run"], second["job"], 2)
    result = await api.get(c, "/decision")
    assert result.status_code == 200, result.text
    assert result.json()["availability"] == "recorded"
