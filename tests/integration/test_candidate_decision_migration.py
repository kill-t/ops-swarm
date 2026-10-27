"""真实 PostgreSQL 核对候选决定原生命令、历史来源和原子事故迁移。"""

import asyncio
import copy
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from tests.api import test_candidate_approval_api as approvals
from tests.api.test_human_wait_api import command as human_command
from tests.api.test_human_wait_api import post
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.integration import test_candidate_approval_migration as requests
from tests.task_facts_helpers import BASE_TIME

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ROUTE = requests.ROUTE + "/decision"
encoded = requests.encoded
digest = requests.digest


def canonical_command():
    return {
        "action": "approve",
        "request_id": str(UUID(int=1)),
        "plan_content_sha256": "a" * 64,
        "expected_incident_version": 3,
        "acknowledge_unresolved_conditions": True,
        "idempotency_key": "native-candidate-decision",
        "reason": "已阅读原证据、前置条件与未解问题后独立决定",
    }


async def authorize(session, values, *, invalid=None):
    await session.execute(
        text(
            "INSERT INTO operator_authorizations (id,policy_version,subject_id,credential_id,"
            "role,permissions,operation,method,route,resources,body_sha256,authorized_at) "
            "VALUES (:authorization,'operator-auth-v1','migration-approver','migration',:role,"
            "CAST(:permissions AS jsonb),:operation,'POST',:route,CAST(:resources AS jsonb),"
            ":body_hash,:now)"
        ),
        {
            **values,
            "role": "engineer" if invalid == "role" else "approver",
            "permissions": encoded([] if invalid == "permission" else ["decide_approval"]),
            "operation": "create_candidate_approval_request"
            if invalid == "operation"
            else "decide_candidate_approval",
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
            "INSERT INTO candidate_decision_commands (authorization_id,investigation_run_id,"
            "candidate_job_id,subject_id,command,raw_body,command_sha256,outcome,http_status,"
            "decision_id,error_code,error_message,created_at) VALUES (:authorization,:run,:job,"
            ":subject,CAST(:command AS jsonb),:raw,:command_hash,:outcome,:status,:decision,"
            ":error,:message,:now)"
        ),
        {
            **values,
            "outcome": outcome,
            "status": {"error": 404, "applied": 201, "replayed": 200}[outcome],
            "decision": None if outcome == "error" else values["id"],
            "error": "candidate_decision_run_not_found" if outcome == "error" else None,
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
        "subject": "another-approver" if invalid == "subject" else "migration-approver",
        "command": encoded(body),
        "command_hash": "c" * 64 if invalid == "command_hash" else digest(encoded(body)),
        "raw": raw,
        "now": BASE_TIME,
    }
    async with factory() as session, session.begin():
        await authorize(session, values, invalid=invalid)
        await insert_audit(session, values)


async def test_0075_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, requests.config())
    try:
        await asyncio.to_thread(command.downgrade, requests.config(), "0074_candidate_approval")
        async with database_session_factory() as session:
            assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
                "0074_candidate_approval"
            )
            for table in ("candidate_approval_decisions", "candidate_decision_commands"):
                assert (
                    await session.scalar(text("SELECT to_regclass(:name)"), {"name": table}) is None
                )
        await asyncio.to_thread(command.upgrade, requests.config(), "head")
        await asyncio.to_thread(command.check, requests.config())
    finally:
        await asyncio.to_thread(command.upgrade, requests.config(), "head")


async def test_0075_deferred_atomic_guards_and_legacy_isolation(database_session_factory):
    async with database_session_factory() as session:
        actual = dict(
            (
                await session.execute(
                    text(
                        "SELECT tgname,tgdeferrable FROM pg_trigger WHERE tgname IN "
                        "('candidate_approval_decisions_guard','candidate_decision_commands_guard',"
                        "'candidate_approval_decisions_state','candidate_decision_commands_state')"
                    )
                )
            ).all()
        )
        assert actual == {
            "candidate_approval_decisions_guard": False,
            "candidate_decision_commands_guard": False,
            "candidate_approval_decisions_state": True,
            "candidate_decision_commands_state": True,
        }
        assert (
            await session.scalar(
                text(
                    "SELECT count(*) FROM information_schema.columns WHERE table_name IN "
                    "('action_plans','approval_requests','operation_executions') "
                    "AND column_name LIKE '%candidate%'"
                )
            )
            == 0
        )


@pytest.mark.parametrize(
    "invalid", ["role", "permission", "operation", "route", "run", "job", "body_hash", "subject"]
)
async def test_0075_refusal_requires_precise_trusted_admission(database_session_factory, invalid):
    with pytest.raises(DBAPIError, match="精确原始正文或同源可信准入"):
        await insert_refusal(database_session_factory, invalid=invalid)


@pytest.mark.parametrize(
    "field,value",
    [
        ("action", "reject"),
        ("request_id", str(UUID(int=2))),
        ("plan_content_sha256", "b" * 64),
        ("expected_incident_version", 4),
        ("idempotency_key", "another-valid-key"),
        ("reason", "另一份未经授权的决定理由"),
    ],
)
async def test_0075_raw_canonical_precise_binding(database_session_factory, field, value):
    raw = canonical_command()
    altered = {**raw, field: value}
    if field == "action":
        altered["acknowledge_unresolved_conditions"] = False
    with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
        await insert_refusal(database_session_factory, raw=raw, canonical=altered)


@pytest.mark.parametrize("token", ["3.0", "3e0", '"3"', "true"])
async def test_0075_version_is_strict_integer(database_session_factory, token):
    body = canonical_command()
    raw = encoded(body).replace(
        '"expected_incident_version":3', '"expected_incident_version":' + token
    )
    with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
        await insert_refusal(database_session_factory, raw=raw, canonical=body)


@pytest.mark.parametrize(
    "change",
    [
        {"action": "execute"},
        {"request_id": "invalid"},
        {"plan_content_sha256": "invalid"},
        {"expected_incident_version": 0},
        {"execution_eligible": True},
        {"acknowledge_unresolved_conditions": False},
        {"acknowledge_unresolved_conditions": "true"},
        {"acknowledge_unresolved_conditions": 1},
        {"reason": "\u3000"},
    ],
)
async def test_0075_invalid_contract_cannot_create_even_error_audit(
    database_session_factory, change
):
    with pytest.raises(DBAPIError):
        await insert_refusal(database_session_factory, canonical={**canonical_command(), **change})


async def test_0075_normalization_and_negative_default_ack(database_session_factory):
    body = canonical_command()
    raw = {**body}
    for field in ("reason", "plan_content_sha256", "idempotency_key"):
        raw[field] = "\u3000\u00a0 " + raw[field] + " \u2003"
    await insert_refusal(database_session_factory, raw=raw, canonical=body)
    body = {**body, "action": "reject", "acknowledge_unresolved_conditions": False}
    raw = {**body}
    raw.pop("acknowledge_unresolved_conditions")
    await insert_refusal(database_session_factory, raw=raw, canonical=body)


async def test_0075_refusal_hash_and_immutable_safe_downgrade(database_session_factory):
    with pytest.raises(DBAPIError, match="有界精确规范来源绑定"):
        await insert_refusal(database_session_factory, invalid="command_hash")
    await insert_refusal(database_session_factory)
    for mutation in (
        "UPDATE candidate_decision_commands SET error_message='篡改'",
        "DELETE FROM candidate_decision_commands",
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with database_session_factory() as session, session.begin():
                await session.execute(text(mutation))
    with pytest.raises(Exception, match="不能删除已保存的候选决定事实"):
        await asyncio.to_thread(command.downgrade, requests.config(), "0074_candidate_approval")
    async with database_session_factory() as session:
        assert await session.scalar(text("SELECT version_num FROM alembic_version")) == (
            "0085_candidate_convergence"
        )


async def ready_decision(c, *, kind="terminal_handoff", action="approve"):
    await approvals.ready(c, kind=kind)
    response = await approvals.create(c)
    assert response.status_code == 201, response.text
    request = response.json()["request"]["record"]
    async with c.db() as session:
        source_hash = await session.scalar(
            text(
                "SELECT source_content_sha256 FROM investigation_candidate_evaluation_jobs "
                "WHERE id=:job"
            ),
            {"job": c.job_id},
        )
        # 从数据库读取实际起点，不能使用创建阶段已经过期的 run 快照。
        started_at = await session.scalar(
            text("SELECT started_at FROM investigation_runs WHERE id=:run"),
            {"run": c.started.run.id},
        )
        assert started_at is not None
    from datetime import datetime

    expires = datetime.fromisoformat(request["expires_at"])
    body = {
        **canonical_command(),
        "action": action,
        "request_id": request["id"],
        "plan_content_sha256": request["plan_content_sha256"],
        "expected_incident_version": request["incident_version"],
        "acknowledge_unresolved_conditions": action == "approve",
    }
    outcome = {"approve": "approved", "reject": "rejected", "expire": "expired"}[action]
    content = {
        "version": "candidate-approval-decision-content-v1",
        "request_id": request["id"],
        "plan_id": request["plan_id"],
        "plan_content_sha256": request["plan_content_sha256"],
        "candidate_job_id": str(c.job_id),
        "source_content_sha256": source_hash,
        "wait_version": request["wait_version"],
        "outcome": outcome,
        **{
            key: c.plan_record["content"][key]
            for key in ("evidence_ids", "preconditions", "unresolved_questions")
        },
        "execution_eligible": False,
    }
    return {
        "id": uuid5(UUID(request["id"]), "candidate-approval-decision:v1"),
        "request": UUID(request["id"]),
        "run": c.started.run.id,
        "job": c.job_id,
        "subject": "migration-approver",
        "authorization": uuid4(),
        "command": encoded(body),
        "raw": encoded(body),
        "command_hash": digest(encoded(body)),
        "key": body["idempotency_key"],
        "content": encoded(content),
        "content_hash": digest(encoded(content)),
        "plan_hash": request["plan_content_sha256"],
        "previous": request["incident_version"],
        "incident": request["incident_version"] + 1,
        "incident_id": c.started.run.incident_id,
        "now": expires if action == "expire" else c.clock.current,
        "expires": expires,
        "outcome": outcome,
        "action": action,
        "reason": body["reason"],
        "status": "mitigating" if action == "approve" else "investigating",
    }


async def transition(session, values, **changes):
    await session.execute(
        text(
            "UPDATE incidents SET status=:status,version=:incident,updated_at=:now "
            "WHERE id=:incident_id"
        ),
        values,
    )
    return await session.scalar(
        text(
            "INSERT INTO incident_events (incident_id,event_type,actor_type,actor_id,from_status,"
            "to_status,reason,incident_version,payload,occurred_at) VALUES "
            "(:incident_id,'incident.status_changed',:actor_type,:subject,:from_status,:status,"
            ":reason,:incident,CAST(:payload AS jsonb),:now) RETURNING id"
        ),
        {
            "actor_type": "human",
            "from_status": "awaiting_approval",
            "payload": encoded(
                {
                    "action": {
                        "approve": "approve_plan",
                        "reject": "reject_plan",
                        "expire": "expire_approval",
                    }[values["action"]]
                }
            ),
            **values,
            **changes,
        },
    )


async def insert_decision(session, values):
    await session.execute(
        text(
            "INSERT INTO candidate_approval_decisions (id,investigation_run_id,candidate_job_id,"
            "request_id,plan_content_sha256,previous_incident_version,incident_version,event_cursor,"
            "outcome,policy_version,decided_by_id,authorization_id,idempotency_key,command_sha256,"
            "content,content_sha256,created_at) VALUES "
            "(:id,:run,:job,:request,:plan_hash,:previous,"
            ":incident,:event,:outcome,'candidate-approval-decision-v1',:subject,:authorization,"
            ":key,:command_hash,CAST(:content AS jsonb),:content_hash,:now)"
        ),
        values,
    )


async def apply(factory, values, *, audit=True, event_changes=None, lock_timeout=False):
    async with factory() as session, session.begin():
        if lock_timeout:
            await session.execute(text("SET LOCAL lock_timeout = '1s'"))
        await authorize(session, values)
        values["event"] = await transition(session, values, **(event_changes or {}))
        await insert_decision(session, values)
        if audit:
            await insert_audit(session, values, outcome="applied")


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
@pytest.mark.parametrize("action", ["approve", "reject", "expire"])
async def test_0075_native_atomic_decision_two_sources(wait_context, kind, action):
    c = wait_context
    values = await ready_decision(c, kind=kind, action=action)
    run = await c.store.get_run(c.started.run.id)
    work = await c.store.get_work_item(c.started.work_item.id)
    attempts = await c.store.list_attempts(c.started.run.id)
    if kind == "terminal_handoff" and action == "approve":
        async with c.db() as holder, holder.begin():
            # 历史授权只读；另一命令持有旧授权时，当前决定不能反向等待授权锁。
            await holder.execute(
                text(
                    "SELECT a.id FROM operator_authorizations a "
                    "JOIN candidate_approval_requests r ON r.authorization_id=a.id "
                    "WHERE r.id=:request FOR UPDATE OF a"
                ),
                values,
            )
            await apply(c.db, values, lock_timeout=True)
    else:
        await apply(c.db, values)
    assert await c.store.get_run(c.started.run.id) == run
    assert await c.store.get_work_item(c.started.work_item.id) == work
    assert await c.store.list_attempts(c.started.run.id) == attempts
    async with c.db() as session:
        assert await session.scalar(text("SELECT count(*) FROM candidate_approval_decisions")) == 1
        assert (
            await session.scalar(text("SELECT status FROM incidents WHERE id=:incident_id"), values)
            == values["status"]
        )
        for table in ("action_plans", "approval_requests", "operation_executions"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    for mutation in (
        "UPDATE candidate_approval_decisions SET outcome='rejected'",
        "DELETE FROM candidate_approval_decisions",
        "UPDATE incident_events SET reason='篡改' WHERE id=:event",
        "DELETE FROM incident_events WHERE id=:event",
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with c.db() as session, session.begin():
                await session.execute(text(mutation), values)
    with pytest.raises(Exception, match="不能删除已保存的候选决定事实"):
        await asyncio.to_thread(command.downgrade, requests.config(), "0074_candidate_approval")


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0075_missing_audit_rolls_back_everything(wait_context):
    c = wait_context
    values = await ready_decision(c)
    with pytest.raises(DBAPIError, match="同事务精确成功命令闭环"):
        await apply(c.db, values, audit=False)
    async with c.db() as session:
        assert await session.scalar(text("SELECT count(*) FROM candidate_approval_decisions")) == 0
        assert (
            await session.scalar(text("SELECT status FROM incidents WHERE id=:incident_id"), values)
            == "awaiting_approval"
        )


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0075_forged_event_version_content_and_deadline_rejected(wait_context):
    c = wait_context
    original = await ready_decision(c)
    mutations = [
        "actor",
        "reason",
        "action",
        "version",
        "previous",
        "plan_hash",
        "content",
        "content_hash",
        "deadline",
        "premature_expire",
        "old_event",
    ]
    for mutation in mutations:
        values = copy.deepcopy(original)
        if mutation == "premature_expire":
            import json

            values["action"] = "expire"
            values["outcome"] = "expired"
            values["status"] = "investigating"
            command_body = {
                **json.loads(values["command"]),
                "action": "expire",
                "acknowledge_unresolved_conditions": False,
            }
            content = {**json.loads(values["content"]), "outcome": "expired"}
            values.update(
                command=encoded(command_body),
                raw=encoded(command_body),
                command_hash=digest(encoded(command_body)),
                content=encoded(content),
                content_hash=digest(encoded(content)),
                now=values["expires"],
            )
        changes = {
            "actor": {"actor_type": "system"},
            "reason": {"reason": "未经此次正文授权的理由"},
            "action": {"payload": encoded({"action": "request_approval"})},
        }.get(mutation, {})
        if mutation == "version":
            values["incident"] += 1
        elif mutation == "previous":
            values["previous"] += 1
        elif mutation == "plan_hash":
            values["plan_hash"] = "b" * 64
        elif mutation == "content":
            import json

            content = {**json.loads(values["content"]), "unresolved_questions": []}
            values["content"] = encoded(content)
            values["content_hash"] = digest(encoded(content))
        elif mutation == "content_hash":
            values["content_hash"] = "b" * 64
        elif mutation == "deadline":
            values["now"] = values["expires"]
        elif mutation == "premature_expire":
            values["now"] -= timedelta(microseconds=1)
        if mutation == "old_event":
            async with c.db() as session, session.begin():
                values["event"] = await transition(session, values)
            with pytest.raises(DBAPIError):
                async with c.db() as session, session.begin():
                    await authorize(session, values)
                    await insert_decision(session, values)
                    await insert_audit(session, values, outcome="applied")
        else:
            with pytest.raises(DBAPIError):
                await apply(c.db, values, event_changes=changes)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0075_transition_uses_actual_old_state(wait_context):
    c = wait_context
    values = await ready_decision(c)
    for status, version in (
        ("investigating", values["previous"]),
        ("awaiting_approval", values["previous"] + 3),
    ):
        async with c.db() as session, session.begin():
            await session.execute(
                text("UPDATE incidents SET status=:status,version=:version WHERE id=:incident_id"),
                {**values, "status": status, "version": version},
            )
        with pytest.raises(DBAPIError, match="真实 awaiting_approval 状态及相邻版本"):
            await apply(c.db, values)


@pytest.mark.parametrize("wait_context", ["human_clean_with_change"], indirect=True)
@pytest.mark.parametrize("action", ["approve", "reject", "expire"])
@pytest.mark.parametrize("input_action", ["supplement", "cancel"])
async def test_0075_negative_can_converge_stale_or_cancelled_input(
    wait_context, action, input_action
):
    c = wait_context
    values = await ready_decision(c, kind="human_route", action=action)
    response = await post(
        c, human_command(c, action=input_action, version=2, key="native-decision-input-change")
    )
    assert response.status_code == 201, response.text
    if action == "approve":
        with pytest.raises(DBAPIError, match="原输入已补充、取消或不再适用"):
            await apply(c.db, values)
    else:
        await apply(c.db, values)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("action", ["approve", "reject", "expire"])
async def test_0075_negative_does_not_require_current_budget_capacity(wait_context, action):
    c = wait_context
    values = await ready_decision(c, action=action)
    async with c.db() as session, session.begin():
        # 只追加未产出业务结果的 unknown 审计，不篡改原成功调用来伪造累计超额。
        await session.execute(text("SET LOCAL session_replication_role = replica"))
        await session.execute(
            text("""
            INSERT INTO investigation_call_reservations
            SELECT extra.* FROM investigation_call_reservations source
            CROSS JOIN generate_series(40,59) slot
            CROSS JOIN LATERAL jsonb_populate_record(
              NULL::investigation_call_reservations,
              to_jsonb(source)||jsonb_build_object(
                'id',gen_random_uuid(),'candidate_attempt_id',NULL,
                'human_review_attempt_id',NULL,'task_id',NULL,'task_attempt_id',NULL,
                'operation','synthesize','invocation_index',slot,
                'sequence',(SELECT max(sequence) FROM investigation_call_reservations
                            WHERE investigation_run_id=:run)+slot-39,
                'idempotency_key',source.investigation_attempt_id::text||':root:synthesize:'||slot,
                'status','unknown','error_code','operation_failed',
                'model_usage',NULL,'call_result',NULL,'context_manifest',NULL,
                'reserved_at',:now,'finished_at',:now)) extra
            WHERE source.investigation_run_id=:run AND source.operation='evaluate_candidate'
        """),
            values,
        )
        await session.execute(text("SET LOCAL session_replication_role = origin"))
    from opsswarm.approval.candidate.store import CandidateApprovalStore
    from opsswarm.approval.decision.store import CandidateDecisionStore

    c.clock.current = values["now"]
    original = await CandidateApprovalStore(c.db, clock=c.clock).get(values["run"], c.job_id)
    assert original.record is not None
    if action == "approve":
        with pytest.raises(DBAPIError, match="累计额度|累计模型预算"):
            await apply(c.db, values)
    else:
        await apply(c.db, values)
        saved = await CandidateDecisionStore(c.db, clock=c.clock).get(values["run"], c.job_id)
        assert saved.record is not None and saved.availability == "recorded"


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0075_negative_still_requires_exact_historical_chain(wait_context):
    c = wait_context
    values = await ready_decision(c, action="reject")
    mutations = {
        "call_usage_shape": "UPDATE investigation_call_reservations SET model_usage="
        "jsonb_build_object('input_tokens',1,'output_tokens',0) "
        "WHERE investigation_run_id=:run AND operation='evaluate_candidate'",
        "call_usage_allocation": "UPDATE investigation_call_reservations SET model_usage="
        "jsonb_build_object('input_tokens',99999999,'output_tokens',0,'total_tokens',99999999) "
        "WHERE investigation_run_id=:run AND operation='evaluate_candidate'",
        "source": "UPDATE investigation_candidate_evaluation_jobs "
        "SET source=jsonb_set(source,'{evidence}','[]'::jsonb) WHERE id=:job",
        "plan": "UPDATE candidate_action_plans "
        "SET content=jsonb_set(content,'{unresolved_questions}','[]'::jsonb) "
        "WHERE candidate_job_id=:job",
        "request_event": "UPDATE incident_events SET reason='替换原请求理由' WHERE id="
        "(SELECT event_cursor FROM candidate_approval_requests WHERE id=:request)",
        "request_audit": "UPDATE candidate_approval_commands SET raw_body='{}' "
        "WHERE request_id=:request",
    }
    for mutation in mutations.values():
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                # 管理连接制造损坏历史，随后恢复守卫；负面决定仍须有完整来源。
                await session.execute(text("SET LOCAL session_replication_role = replica"))
                await session.execute(text(mutation), values)
                await session.execute(text("SET LOCAL session_replication_role = origin"))
                await authorize(session, values)
                values["event"] = await transition(session, values)
                await insert_decision(session, values)
                await insert_audit(session, values, outcome="applied")
        async with c.db() as session:
            assert (
                await session.scalar(text("SELECT count(*) FROM candidate_approval_decisions")) == 0
            )


@pytest.mark.parametrize("form", ["compact", "braces", "urn", "spaces", "uppercase_urn"])
async def test_0075_uuid_normalization_matches_contract(database_session_factory, form):
    from opsswarm.approval.decision.contracts import DecisionCommand

    body = canonical_command()
    assert DecisionCommand.model_validate_json(encoded(body)).model_dump(mode="json") == body
    request = body["request_id"]
    raw = {
        **body,
        "request_id": {
            "compact": request.replace("-", ""),
            "braces": "{" + request + "}",
            "urn": "urn:uuid:" + request,
            "spaces": " " + request + " ",
            "uppercase_urn": "URN:UUID:" + request,
        }[form],
    }
    if form in {"spaces", "uppercase_urn"}:
        with pytest.raises(ValueError):
            DecisionCommand.model_validate_json(encoded(raw))
        with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
            await insert_refusal(database_session_factory, raw=raw, canonical=body)
    else:
        assert DecisionCommand.model_validate_json(encoded(raw)).model_dump(mode="json") == body
        await insert_refusal(database_session_factory, raw=raw, canonical=body)
