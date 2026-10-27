"""隔离 PostgreSQL 验证回流准入的原生来源、预算及同事务审计。"""

import asyncio
import copy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from alembic import command as migration_command
from opsswarm.incidents.convergence.contracts import ConvergenceRecord
from opsswarm.incidents.convergence.tables import CandidateIncidentConvergenceRow
from opsswarm.investigation.budget.queries import read_budget_snapshot
from opsswarm.investigation.context.hashing import content_hash
from opsswarm.investigation.persistence.tables import InvestigationRunRow
from opsswarm.investigation.reinvestigation.contracts import ReinvestigationCommand
from opsswarm.investigation.reinvestigation.policy import make_content
from tests.api import test_reinvestigation_admission_api as api
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.api.test_release_execution_jobs_api import (
    current_fixture_time as current_fixture_time,
)
from tests.integration import test_candidate_approval_migration as old
from tests.integration import test_dynamic_worker_handoff as worker_fixture

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
encoded, digest = old.encoded, old.digest


@pytest.fixture(autouse=True)
def original_deadline_policy(monkeypatch, request, current_fixture_time):
    """仅提交门禁用例从创建时固定较短原策略，不改写运行或生产时钟。"""
    del current_fixture_time
    if "actual_deadline_is_checked_again" not in request.node.name:
        return
    original = worker_fixture.CallBudgetPolicy
    monkeypatch.setattr(
        worker_fixture,
        "CallBudgetPolicy",
        lambda **values: original(**{**values, "max_duration_seconds": 180}),
    )


def refusal():
    body = dict(
        action="request_reinvestigation",
        convergence_sha256="a" * 64,
        expected_incident_version=5,
        idempotency_key="native-reinvestigation-131",
        reason="显式申请有界回流，不分派或重置预算",
    )
    return dict(
        run=uuid4(),
        subject="native-engineer",
        authorization=uuid4(),
        body=body,
        raw=encoded(body),
        now=old.BASE_TIME,
        request=None,
    )


async def authorize(session, values, *, invalid=None):
    await session.execute(
        text(
            "INSERT INTO operator_authorizations (id,policy_version,subject_id,credential_id,"
            "role,permissions,operation,method,route,resources,body_sha256,authorized_at) VALUES "
            "(:authorization,'operator-auth-v1',:subject,'native',:role,"
            "CAST(:permissions AS jsonb),"
            ":operation,'POST',:route,CAST(:resources AS jsonb),:hash,:now)"
        ),
        {
            **values,
            "subject": "other-subject" if invalid == "subject" else values["subject"],
            "role": "viewer" if invalid == "role" else "engineer",
            "permissions": encoded([] if invalid == "permission" else ["manage_incident"]),
            "operation": "manage_incident"
            if invalid == "operation"
            else "request_candidate_reinvestigation",
            "route": "/api/v1/investigations/{run_id}/reinvestigation-admission"
            + ("/commands" if invalid == "route" else ""),
            "resources": encoded(
                dict(run_id=str(uuid4() if invalid == "resource" else values["run"]))
            ),
            "hash": "f" * 64 if invalid == "body_hash" else digest(values["raw"]),
        },
    )


async def insert_request(session, values):
    await session.execute(
        text(
            "INSERT INTO candidate_reinvestigation_admissions "
            "SELECT * FROM jsonb_populate_record(NULL::candidate_reinvestigation_admissions,"
            "CAST(:fact AS jsonb))"
        ),
        dict(fact=encoded(values["fact"])),
    )


async def insert_audit(session, values, *, outcome="applied", error=None, status=None):
    await session.execute(
        text(
            "INSERT INTO candidate_reinvestigation_commands (authorization_id,investigation_run_id,"
            "subject_id,command,raw_body,command_sha256,outcome,http_status,request_id,error_code,"
            "error_message,created_at) VALUES (:authorization,:run,:subject,CAST(:body AS jsonb),"
            ":raw,:hash,:outcome,:status,:request,:error,:message,:now)"
        ),
        {
            **values,
            "body": encoded(values["body"]),
            "hash": digest(encoded(values["body"])),
            "outcome": outcome,
            "status": status or {"applied": 201, "replayed": 200, "error": 409}[outcome],
            "request": None if outcome == "error" else values["request"],
            "error": error,
            "message": "回流准入被业务规则拒绝" if error else None,
        },
    )


async def ready(c, *, kind="terminal_handoff"):
    await api.prepare(c, kind=kind)
    async with c.db() as session:
        run = await session.get(InvestigationRunRow, c.started.run.id)
        source = await session.get(CandidateIncidentConvergenceRow, run.id)
        convergence = ConvergenceRecord(**source.content, content_sha256=source.content_sha256)
        budget = await read_budget_snapshot(session, run)
        body = ReinvestigationCommand(
            convergence_sha256=source.content_sha256,
            expected_incident_version=convergence.after_version,
            idempotency_key="native-reinvestigation-131",
            reason="原预算保守占用并显式申请一次回流",
        )
        authorization = uuid4()
        content = make_content(
            run_id=run.id,
            incident_id=run.incident_id,
            convergence=convergence,
            budget=budget,
            command=body,
            subject_id="native-engineer",
            authorization_id=authorization,
            now=c.clock(),
        )
    return dict(
        run=run.id,
        subject=content.subject_id,
        authorization=authorization,
        body=body.model_dump(mode="json"),
        raw=encoded(body.model_dump(mode="json")),
        now=content.created_at,
        request=content.id,
        fact=dict(
            id=str(content.id),
            investigation_run_id=str(run.id),
            incident_id=str(run.incident_id),
            authorization_id=str(authorization),
            content=content.model_dump(mode="json"),
            content_sha256=content_hash(content),
            created_at=content.created_at.isoformat(),
        ),
    )


async def save(session, values, *, audit=True):
    await authorize(session, values)
    await insert_request(session, values)
    if audit:
        await insert_audit(session, values)


async def test_0086_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(migration_command.check, old.config())
    try:
        await asyncio.to_thread(
            migration_command.downgrade, old.config(), "0085_candidate_convergence"
        )
        async with database_session_factory() as session:
            assert (
                await session.scalar(
                    text("SELECT to_regclass('candidate_reinvestigation_admissions')")
                )
                is None
            )
            assert (
                await session.scalar(
                    text("SELECT to_regprocedure('guard_reinvestigation_request()')")
                )
                is None
            )
        await asyncio.to_thread(migration_command.upgrade, old.config(), "head")
        await asyncio.to_thread(migration_command.check, old.config())
    finally:
        await asyncio.to_thread(migration_command.upgrade, old.config(), "head")


@pytest.mark.parametrize(
    "invalid", ["role", "permission", "subject", "operation", "route", "resource", "body_hash"]
)
async def test_0086_refusal_audit_requires_precise_authorization(database_session_factory, invalid):
    values = refusal()
    with pytest.raises(DBAPIError, match="可信授权|原始正文"):
        async with database_session_factory() as session, session.begin():
            await authorize(session, values, invalid=invalid)
            await insert_audit(
                session, values, outcome="error", error="reinvestigation_run_not_found", status=404
            )


async def test_0086_native_refusal_must_match_real_business_state(database_session_factory):
    values = refusal()
    async with database_session_factory() as session, session.begin():
        await authorize(session, values)
        await insert_audit(
            session, values, outcome="error", error="reinvestigation_run_not_found", status=404
        )
    values = refusal()
    with pytest.raises(DBAPIError, match="合理业务拒绝"):
        async with database_session_factory() as session, session.begin():
            await authorize(session, values)
            await insert_audit(
                session, values, outcome="error", error="reinvestigation_already_exists", status=409
            )


async def test_0086_raw_body_normalization_matches_contract_and_rejects_forgery(
    database_session_factory,
):
    for mode in ("default_action", "strip_text"):
        values = refusal()
        raw = copy.deepcopy(values["body"])
        if mode == "default_action":
            raw.pop("action")
        else:
            raw["reason"] = " \t" + raw["reason"] + "\n\u3000"
            raw["idempotency_key"] = "\t" + raw["idempotency_key"] + " "
        values["raw"] = encoded(raw)
        async with database_session_factory() as session, session.begin():
            await authorize(session, values)
            await insert_audit(
                session, values, outcome="error", error="reinvestigation_run_not_found", status=404
            )
    for mode in ("extra", "action", "integer", "boolean", "reason", "command"):
        values = refusal()
        raw = copy.deepcopy(values["body"])
        if mode == "extra":
            raw["extra"] = "injected"
        elif mode == "action":
            raw["action"] = "execute"
        elif mode == "integer":
            raw["expected_incident_version"] = 5.0
        elif mode == "boolean":
            raw["expected_incident_version"] = True
        elif mode == "reason":
            raw.pop("reason")
        else:
            values["body"]["reason"] = "篡改规范命令并重算摘要"
        values["raw"] = encoded(raw)
        with pytest.raises(DBAPIError, match="原始正文|规范命令"):
            async with database_session_factory() as session, session.begin():
                await authorize(session, values)
                await insert_audit(
                    session,
                    values,
                    outcome="error",
                    error="reinvestigation_run_not_found",
                    status=404,
                )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0086_rehashed_forged_basis_budget_and_policy_are_rejected(wait_context):
    c = wait_context
    values = await ready(c)
    for invalid in (
        "policy_version",
        "id",
        "root_run_id",
        "previous_run_id",
        "budget_run_id",
        "max_reflows",
        "reflow_number",
        "dispatch_status",
        "incident_version",
        "convergence_content_sha256",
        "budget_calls",
        "budget_cost",
        "budget_unknown",
        "budget_policy",
        "extra",
    ):
        forged = copy.deepcopy(values)
        content = forged["fact"]["content"]
        if invalid == "id":
            content["id"] = forged["fact"]["id"] = str(uuid4())
        elif invalid in {"root_run_id", "previous_run_id", "budget_run_id"}:
            content[invalid] = str(uuid4())
        elif invalid in {"max_reflows", "reflow_number", "incident_version"}:
            content[invalid] += 1
        elif invalid == "convergence_content_sha256":
            content[invalid] = "f" * 64
        elif invalid == "budget_calls":
            content["budget"]["calls"] = content["budget"]["calls"][1:]
        elif invalid == "budget_cost":
            content["budget"]["model_usage"]["committed_cost_usd"] = "0"
        elif invalid == "budget_unknown":
            content["budget"]["model_usage"]["unknown_model_calls"] += 1
        elif invalid == "budget_policy":
            content["budget"]["policy"]["max_model_calls"] += 1
        else:
            content[invalid] = "injected"
        forged["fact"]["content_sha256"] = digest(encoded(content))
        with pytest.raises(DBAPIError, match="回流准入"):
            async with c.db() as session, session.begin():
                await save(session, forged)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0086_request_requires_same_transaction_applied_audit(wait_context):
    c = wait_context
    values = await ready(c)
    with pytest.raises(DBAPIError, match="成功命令历史|同事务"):
        async with c.db() as session, session.begin():
            await save(session, values, audit=False)
    async with c.db() as session:
        assert (
            await session.scalar(text("SELECT count(*) FROM candidate_reinvestigation_admissions"))
            == 0
        )
    async with c.db() as session, session.begin(), session.begin_nested():
        await save(session, values)
    with pytest.raises(Exception, match="不能删除已保存的回流准入"):
        await asyncio.to_thread(
            migration_command.downgrade, old.config(), "0085_candidate_convergence"
        )
    # 仅隔离测试模拟先前被特权破坏的孤立事实；之后重新启用守卫再尝试跨事务补齐。
    async with c.db() as session, session.begin():
        await session.execute(
            text(
                "ALTER TABLE candidate_reinvestigation_commands "
                "DISABLE TRIGGER reinvestigation_command_guard"
            )
        )
        await session.execute(text("DELETE FROM candidate_reinvestigation_commands"))
        await session.execute(
            text(
                "ALTER TABLE candidate_reinvestigation_commands "
                "ENABLE TRIGGER reinvestigation_command_guard"
            )
        )
    with pytest.raises(DBAPIError, match="同事务保存"):
        async with c.db() as session, session.begin():
            await insert_audit(session, values)


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_0086_native_budget_matches_python_and_success_is_immutable(wait_context, kind):
    c = wait_context
    values = await ready(c, kind=kind)
    async with c.db() as session:
        native = await session.scalar(
            text("SELECT reinvestigation_budget_snapshot(:run,:now)"), values
        )
        assert native == values["fact"]["content"]["budget"]
        frozen = list(await session.scalars(select(InvestigationRunRow)))
        assert len(frozen) == 1
    async with c.db() as session, session.begin():
        await save(session, values)
    replay = {**values, "authorization": uuid4(), "now": c.clock()}
    async with c.db() as session, session.begin():
        await authorize(session, replay)
        await insert_audit(session, replay, outcome="replayed")
    for sql in (
        "UPDATE candidate_reinvestigation_admissions SET content_sha256=repeat('f',64)",
        "DELETE FROM candidate_reinvestigation_admissions",
        "UPDATE candidate_reinvestigation_commands SET http_status=200",
        "DELETE FROM candidate_reinvestigation_commands",
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with c.db() as session, session.begin():
                await session.execute(text(sql))
    for sql in (
        "DELETE FROM investigation_call_reservations WHERE sequence=1",
        "UPDATE investigation_call_reservations SET sequence=150 WHERE sequence=1",
        "UPDATE investigation_call_reservations SET investigation_run_id=gen_random_uuid(),"
        "sequence=150 WHERE sequence=1",
    ):
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text(sql))
    for field in ("reserved_at", "sequence"):
        with pytest.raises(DBAPIError, match="新增调用必须晚于准入"):
            async with c.db() as session, session.begin():
                # 只在回滚事务中跳过旧账本的阶段守卫，直接检查131对未来前缀的保护。
                await session.execute(
                    text("ALTER TABLE investigation_call_reservations DISABLE TRIGGER USER")
                )
                await session.execute(
                    text(
                        "ALTER TABLE investigation_call_reservations "
                        "ENABLE TRIGGER reinvestigation_call_prefix_guard"
                    )
                )
                identifier = uuid4()
                await session.execute(
                    text(
                        "INSERT INTO investigation_call_reservations SELECT "
                        "(jsonb_populate_record(NULL::investigation_call_reservations,"
                        "to_jsonb(call)||jsonb_build_object('id',CAST(:id AS text),'sequence',150,"
                        "'invocation_index',99,'idempotency_key',call.investigation_attempt_id::text"
                        "||':'||call.task_attempt_id::text||':'||call.operation||':'||99::text,"
                        "'reserved_at',CAST(:now AS timestamptz)+interval '1 second',"
                        "'finished_at',CAST(:now AS timestamptz)+interval '1 second'))).* "
                        "FROM investigation_call_reservations call WHERE kind='tool' "
                        "ORDER BY sequence LIMIT 1"
                    ),
                    {**values, "id": identifier},
                )
                expression = (
                    ":now"
                    if field == "reserved_at"
                    else (
                        "(SELECT jsonb_array_length(content->'budget'->'calls') "
                        "FROM candidate_reinvestigation_admissions WHERE id=:run)"
                    )
                )
                await session.execute(
                    text(
                        f"UPDATE investigation_call_reservations SET {field}={expression} "
                        "WHERE id=:id"
                    ),
                    {**values, "id": identifier},
                )
    async with c.db() as session:
        await session.execute(
            text(
                "SELECT assert_reinvestigation_request(item,TRUE,FALSE) "
                "FROM candidate_reinvestigation_admissions item"
            )
        )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0086_actual_deadline_is_checked_again_at_commit(wait_context):
    c = wait_context
    values = await ready(c)
    frozen = await api.frozen(c)
    deadline = datetime.fromisoformat(values["fact"]["content"]["budget"]["deadline"])
    assert datetime.now(UTC) < deadline
    with pytest.raises(DBAPIError, match="原期限"):
        async with c.db() as session, session.begin():
            await save(session, values)
            # 保持事务直到真实数据库时钟超期；请求和成功审计均应由延迟门禁回滚。
            while (remaining := (deadline - datetime.now(UTC)).total_seconds()) > -0.1:
                await session.execute(
                    text("SELECT pg_sleep(:delay)"), dict(delay=min(10, remaining + 0.15))
                )
    assert await api.counts(c) == (0, 0)
    assert await api.frozen(c) == frozen
