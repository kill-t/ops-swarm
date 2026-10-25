"""真实 PostgreSQL 核对候选检查命令、观测与不可变事务闭环。"""

import asyncio
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.integration import test_candidate_approval_migration as requests
from tests.task_facts_helpers import BASE_TIME

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ROUTE = "/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-preflights"
encoded = requests.encoded
digest = requests.digest


def canonical_command():
    return {
        "decision_id": str(UUID(int=1)),
        "decision_content_sha256": "a" * 64,
        "expected_incident_version": 3,
        "idempotency_key": "native-candidate-decision",
        "reason": "已阅读原证据、前置条件与未解问题后独立决定",
    }


async def authorize(session, values, *, invalid=None):
    await session.execute(
        text(
            "INSERT INTO operator_authorizations (id,policy_version,subject_id,credential_id,"
            "role,permissions,operation,method,route,resources,body_sha256,authorized_at) "
            "VALUES (:authorization,'operator-auth-v1','migration-executor','migration',:role,"
            "CAST(:permissions AS jsonb),:operation,'POST',:route,CAST(:resources AS jsonb),"
            ":body_hash,:now)"
        ),
        {
            **values,
            "role": "engineer" if invalid == "role" else "executor",
            "permissions": encoded([] if invalid == "permission" else ["execute_action"]),
            "operation": "create_candidate_approval_request"
            if invalid == "operation"
            else "check_candidate_execution",
            "route": ROUTE + "/commands" if invalid == "route" else ROUTE,
            "resources": encoded(
                {
                    "run_id": str(uuid4() if invalid == "run" else values["run"]),
                    "job_id": str(uuid4() if invalid == "job" else values["job"]),
                }
            ),
            "body_hash": "b" * 64 if invalid == "body_hash" else digest(values["raw"]),
        },
    )


async def insert_audit(session, values, *, outcome="error"):
    await session.execute(
        text(
            "INSERT INTO candidate_preflight_commands (authorization_id,investigation_run_id,"
            "candidate_job_id,subject_id,command,raw_body,command_sha256,outcome,http_status,"
            "preflight_id,error_code,error_message,created_at) VALUES (:authorization,:run,:job,"
            ":subject,CAST(:command AS jsonb),:raw,:command_hash,:outcome,:status,:decision,"
            ":error,:message,:now)"
        ),
        {
            **values,
            "outcome": outcome,
            "status": {"error": 404, "applied": 201, "replayed": 200}[outcome],
            "decision": None if outcome == "error" else values["id"],
            "error": "candidate_preflight_run_not_found" if outcome == "error" else None,
            "message": "目标运行不存在" if outcome == "error" else None,
        },
    )


async def insert_refusal(factory, *, invalid=None, raw=None, canonical=None):
    body = canonical_command() if canonical is None else canonical
    raw = encoded(body) if raw is None else raw if isinstance(raw, str) else encoded(raw)
    values = {
        "authorization": uuid4(),
        "run": uuid4(),
        "job": uuid4(),
        "subject": "another-executor" if invalid == "subject" else "migration-executor",
        "command": encoded(body),
        "command_hash": "c" * 64 if invalid == "command_hash" else digest(encoded(body)),
        "raw": raw,
        "now": BASE_TIME,
    }
    async with factory() as session, session.begin():
        await authorize(session, values, invalid=invalid)
        await insert_audit(session, values)


async def test_0076_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, requests.config())
    try:
        await asyncio.to_thread(command.downgrade, requests.config(), "0075_candidate_decision")
        async with database_session_factory() as session:
            for table in ("candidate_execution_preflights", "candidate_preflight_commands"):
                assert (
                    await session.scalar(text("SELECT to_regclass(:name)"), {"name": table}) is None
                )
        await asyncio.to_thread(command.upgrade, requests.config(), "head")
        await asyncio.to_thread(command.check, requests.config())
    finally:
        await asyncio.to_thread(command.upgrade, requests.config(), "head")


async def test_0076_deferred_guards(database_session_factory):
    async with database_session_factory() as session:
        actual = dict(
            (
                await session.execute(
                    text(
                        "SELECT tgname,tgdeferrable FROM pg_trigger WHERE tgname IN "
                        "('candidate_execution_preflights_guard','candidate_preflight_commands_guard',"
                        "'candidate_execution_preflights_state','candidate_preflight_commands_state')"
                    )
                )
            ).all()
        )
        assert actual == {
            "candidate_execution_preflights_guard": False,
            "candidate_preflight_commands_guard": False,
            "candidate_execution_preflights_state": True,
            "candidate_preflight_commands_state": True,
        }


@pytest.mark.parametrize(
    "invalid", ["role", "permission", "operation", "route", "run", "job", "body_hash", "subject"]
)
async def test_0076_refusal_requires_precise_trusted_admission(database_session_factory, invalid):
    with pytest.raises(DBAPIError, match="精确原始正文或同源可信准入"):
        await insert_refusal(database_session_factory, invalid=invalid)


@pytest.mark.parametrize(
    "field,value",
    [
        ("decision_id", str(UUID(int=2))),
        ("decision_content_sha256", "b" * 64),
        ("expected_incident_version", 4),
        ("idempotency_key", "another-valid-key"),
        ("reason", "另一份未经授权的检查理由"),
    ],
)
async def test_0076_raw_canonical_precise_binding(database_session_factory, field, value):
    raw = canonical_command()
    with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
        await insert_refusal(database_session_factory, raw=raw, canonical={**raw, field: value})


@pytest.mark.parametrize("token", ["3.0", "3e0", '"3"', "true", "1"])
async def test_0076_version_is_strict_integer(database_session_factory, token):
    body = canonical_command()
    raw = encoded(body).replace(
        '"expected_incident_version":3', '"expected_incident_version":' + token
    )
    with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
        await insert_refusal(database_session_factory, raw=raw, canonical=body)


@pytest.mark.parametrize(
    "change",
    [
        {"decision_id": "invalid"},
        {"decision_content_sha256": "invalid"},
        {"expected_incident_version": 0},
        {"execution_eligible": True},
        {"current_value": 10},
        {"url": "http://localhost"},
        {"reason": ""},
        {"idempotency_key": "bad"},
    ],
)
async def test_0076_invalid_command_rejected(database_session_factory, change):
    with pytest.raises(DBAPIError):
        await insert_refusal(database_session_factory, canonical={**canonical_command(), **change})


async def test_0076_error_audit_immutable_and_cannot_downgrade(database_session_factory):
    await insert_refusal(database_session_factory)
    for sql in (
        "UPDATE candidate_preflight_commands SET error_message='篡改'",
        "DELETE FROM candidate_preflight_commands",
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with database_session_factory() as session, session.begin():
                await session.execute(text(sql))
    with pytest.raises(Exception, match="不能删除已保存的候选检查事实"):
        await asyncio.to_thread(command.downgrade, requests.config(), "0075_candidate_decision")


async def ready_preflight(c):
    """复用完整真实候选批准，后续攻击共享这一份昂贵来源。"""
    import json
    from datetime import timedelta
    from uuid import uuid5

    from opsswarm.execution.candidate.queries import record_id
    from tests.integration import test_candidate_decision_migration as decisions

    approved = await decisions.ready_decision(c)
    await decisions.apply(c.db, approved)
    decision_content = json.loads(approved["content"])
    command_body = {
        **canonical_command(),
        "decision_id": str(approved["id"]),
        "decision_content_sha256": approved["content_hash"],
        "expected_incident_version": approved["incident"],
    }
    step = c.plan_record["content"]["plan"]["steps"][0]
    action = step["action_type"]
    path = {
        "rollback_release": "/__admin/faults/release-500",
        "rollback_configuration": "/__admin/configuration/database-pool",
        "restore_dependency": "/__admin/dependencies/redis",
    }[action]
    observation = {
        "action_type": action,
        "observed_at": c.clock.current.isoformat().replace("+00:00", "Z"),
        "source_path": path,
        "current_value": None,
        "available": None,
        "release_enabled": None,
        "error_code": None,
    }
    blockers = ["unverified_preconditions"]
    if decision_content["unresolved_questions"]:
        blockers.append("unresolved_questions")
    if action == "rollback_release":
        observation["release_enabled"] = True
        blockers.append("runtime_version_unverifiable")
    elif action == "rollback_configuration":
        observation["current_value"] = step["parameters"]["current_value"]
    else:
        observation["available"] = step["parameters"]["expected_available"]
    content = {
        "version": "candidate-execution-preflight-content-v1",
        "decision_id": str(approved["id"]),
        "decision_content_sha256": approved["content_hash"],
        "plan_content_sha256": approved["plan_hash"],
        "step": step,
        "observation": observation,
        "preconditions": decision_content["preconditions"],
        "unresolved_questions": decision_content["unresolved_questions"],
        "blockers": blockers,
        "status": "blocked",
        "execution_eligible": False,
    }
    operation = uuid5(approved["id"], "candidate-operation:v1")
    return {
        "id": record_id(operation, "migration-executor", command_body["idempotency_key"]),
        "operation": operation,
        "decision": approved["id"],
        "decision_hash": approved["content_hash"],
        "run": approved["run"],
        "job": approved["job"],
        "authorization": uuid4(),
        "subject": "migration-executor",
        "key": command_body["idempotency_key"],
        "command": encoded(command_body),
        "raw": encoded(command_body),
        "command_hash": digest(encoded(command_body)),
        "content": encoded(content),
        "content_hash": digest(encoded(content)),
        "incident": approved["incident"],
        "now": c.clock.current,
        "expires": min(approved["expires"], c.clock.current + timedelta(seconds=30)),
        "incident_id": approved["incident_id"],
    }


async def insert_preflight(session, values):
    await session.execute(
        text(
            "INSERT INTO candidate_execution_preflights (id,operation_id,investigation_run_id,"
            "candidate_job_id,decision_id,decision_content_sha256,incident_version,policy_version,"
            "requested_by_id,authorization_id,idempotency_key,command_sha256,content,content_sha256,"
            "created_at,expires_at) VALUES (:id,:operation,:run,:job,:decision,"
            ":decision_hash,:incident,"
            "'candidate-execution-preflight-v1',:subject,:authorization,:key,:command_hash,"
            "CAST(:content AS jsonb),:content_hash,:now,:expires)"
        ),
        values,
    )


async def apply_preflight(factory, values, *, audit=True):
    async with factory() as session, session.begin():
        await session.execute(text("SET LOCAL lock_timeout = '1s'"))
        await authorize(session, values)
        await insert_preflight(session, values)
        if audit:
            await insert_audit(session, values, outcome="applied")


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0076_native_content_atomicity_history_and_immutability(wait_context):
    import json
    from datetime import timedelta

    c = wait_context
    values = await ready_preflight(c)
    run = await c.store.get_run(c.started.run.id)
    work = await c.store.get_work_item(c.started.work_item.id)
    async with c.db() as session:
        events = await session.scalar(text("SELECT count(*) FROM incident_events"))
    for status in ("reserved", "unknown"):
        with pytest.raises(DBAPIError, match="累计额度|累计模型预算"):
            async with c.db() as session, session.begin():
                # 追加独立调用审计，保持原成功候选用量与业务来源完整。
                await session.execute(text("SET LOCAL session_replication_role = replica"))
                await session.execute(
                    text("""
                    INSERT INTO investigation_call_reservations
                    SELECT extra.* FROM investigation_call_reservations source
                    CROSS JOIN generate_series(40,:last_slot) slot
                    CROSS JOIN LATERAL jsonb_populate_record(
                      NULL::investigation_call_reservations,
                      to_jsonb(source)||jsonb_build_object(
                        'id',gen_random_uuid(),'candidate_attempt_id',NULL,
                        'human_review_attempt_id',NULL,'task_id',NULL,'task_attempt_id',NULL,
                        'operation','synthesize','invocation_index',slot,
                        'sequence',(SELECT max(sequence) FROM investigation_call_reservations
                                    WHERE investigation_run_id=:run)+slot-39,
                        'idempotency_key',source.investigation_attempt_id::text||
                                          ':root:synthesize:'||slot,
                        'status',CAST(:status AS text),'error_code',CAST(:error AS text),
                        'model_usage',NULL,'call_result',NULL,'context_manifest',NULL,
                        'reserved_at',:now,'finished_at',CAST(:finished AS timestamptz))) extra
                    WHERE source.investigation_run_id=:run AND source.operation='evaluate_candidate'
                    """),
                    {
                        **values,
                        "last_slot": 40 if status == "reserved" else 59,
                        "status": status,
                        "error": None if status == "reserved" else "operation_failed",
                        "finished": None if status == "reserved" else values["now"],
                    },
                )
                await session.execute(text("SET LOCAL session_replication_role = origin"))
                attacked = {**values, "authorization": uuid4()}
                await authorize(session, attacked)
                await insert_preflight(session, attacked)
    # 每个攻击均在独立回滚事务中使用同一份完整真实来源。
    for field, changed in (
        ("decision_hash", "b" * 64),
        ("incident", values["incident"] + 1),
        ("expires", values["expires"] + timedelta(seconds=1)),
        ("content_hash", "b" * 64),
    ):
        attacked = {**values, field: changed, "authorization": uuid4()}
        with pytest.raises(DBAPIError):
            await apply_preflight(c.db, attacked)
    original = json.loads(values["content"])
    for change in (
        {"execution_eligible": True},
        {"status": "ready"},
        {"blockers": []},
        {"preconditions": []},
        {"step": {**original["step"], "target_name": "other"}},
        {"observation": {**original["observation"], "source_path": "/invalid"}},
        {
            "observation": {
                **original["observation"],
                "observed_at": (values["now"] + timedelta(seconds=1)).isoformat(),
            }
        },
        {"observation": {**original["observation"], "error_code": "timeout"}},
    ):
        content = encoded({**original, **change})
        attacked = {
            **values,
            "authorization": uuid4(),
            "content": content,
            "content_hash": digest(content),
        }
        with pytest.raises(DBAPIError):
            await apply_preflight(c.db, attacked)
    with pytest.raises(DBAPIError, match="同事务精确成功命令闭环"):
        await apply_preflight(c.db, {**values, "authorization": uuid4()}, audit=False)
    # 读取旧批准历史不会反向等待其他事务持有的旧授权锁。
    async with c.db() as holder, holder.begin():
        await holder.execute(
            text(
                "SELECT a.id FROM operator_authorizations a JOIN candidate_approval_decisions d "
                "ON d.authorization_id=a.id WHERE d.id=:decision FOR UPDATE OF a"
            ),
            values,
        )
        await apply_preflight(c.db, values)
    replay = {**values, "authorization": uuid4()}
    async with c.db() as session, session.begin():
        await authorize(session, replay)
        await insert_audit(session, replay, outcome="replayed")
    with pytest.raises(DBAPIError, match="不能降级"):
        async with c.db() as session, session.begin():
            refused = {**values, "authorization": uuid4()}
            await authorize(session, refused)
            await insert_audit(session, refused, outcome="error")
    assert await c.store.get_run(c.started.run.id) == run
    assert await c.store.get_work_item(c.started.work_item.id) == work
    from opsswarm.execution.candidate.store import CandidatePreflightStore

    history = await CandidatePreflightStore(c.db, clock=c.clock).get(values["run"], values["job"])
    assert len(history.items) == 1 and history.items[0].id == values["id"]
    async with c.db() as session:
        assert await session.scalar(text("SELECT count(*) FROM incident_events")) == events
        for table in ("action_plans", "approval_requests", "operation_executions"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
        assert (
            await session.scalar(text("SELECT count(*) FROM candidate_execution_preflights")) == 1
        )
    for sql in (
        "UPDATE candidate_execution_preflights SET content='{}'::jsonb",
        "DELETE FROM candidate_execution_preflights",
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with c.db() as session, session.begin():
                await session.execute(text(sql))
