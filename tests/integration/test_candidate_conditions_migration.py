"""隔离 PostgreSQL 验证核验目录、严格来源与版本事务闭环。"""

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.integration import test_candidate_approval_migration as requests
from tests.integration import test_candidate_preflight_migration as preflights
from tests.task_facts_helpers import BASE_TIME

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]
ROUTE = "/api/v1/investigations/{run_id}/candidate-evaluations/{job_id}/execution-conditions"
encoded = requests.encoded
digest = requests.digest


def canonical_command():
    return {
        "decision_id": str(UUID(int=1)),
        "decision_content_sha256": "a" * 64,
        "expected_incident_version": 3,
        "expected_condition_version": 0,
        "previous_content_sha256": None,
        "items": [
            {
                "condition_id": "b" * 64,
                "status": "unresolved",
                "explanation": "等待可信工程师核验",
                "sources": [],
            }
        ],
        "idempotency_key": "native-condition-verification",
        "reason": "对精确原计划逐项核验",
    }


async def authorize(session, values, *, invalid=None):
    await session.execute(
        text(
            "INSERT INTO operator_authorizations (id,policy_version,subject_id,credential_id,"
            "role,permissions,operation,method,route,resources,body_sha256,authorized_at) "
            "VALUES (:authorization,'operator-auth-v1','migration-engineer','migration',:role,"
            "CAST(:permissions AS jsonb),:operation,'POST',:route,CAST(:resources AS jsonb),"
            ":body_hash,:now)"
        ),
        {
            **values,
            "role": "executor" if invalid == "role" else "engineer",
            "permissions": encoded([] if invalid == "permission" else ["verify_action"]),
            "operation": "check_candidate_execution"
            if invalid == "operation"
            else "verify_candidate_conditions",
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
            "INSERT INTO candidate_condition_commands (authorization_id,investigation_run_id,"
            "candidate_job_id,subject_id,command,raw_body,command_sha256,outcome,http_status,"
            "verification_id,error_code,error_message,created_at) VALUES (:authorization,:run,:job,"
            ":subject,CAST(:command AS jsonb),:raw,:command_hash,:outcome,:status,:verification,"
            ":error,:message,:now)"
        ),
        {
            **values,
            "outcome": outcome,
            "status": {"error": 404, "applied": 201, "replayed": 200}[outcome],
            "verification": None if outcome == "error" else values["id"],
            "error": "candidate_condition_run_not_found" if outcome == "error" else None,
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
        "subject": "another-engineer" if invalid == "subject" else "migration-engineer",
        "command": encoded(body),
        "command_hash": digest(encoded(body)),
        "raw": raw,
        "now": BASE_TIME,
    }
    async with factory() as session, session.begin():
        await authorize(session, values, invalid=invalid)
        await insert_audit(session, values)


async def test_0077_metadata_and_empty_roundtrip(database_session_factory):
    await asyncio.to_thread(command.check, requests.config())
    try:
        await asyncio.to_thread(command.downgrade, requests.config(), "0076_candidate_preflight")
        async with database_session_factory() as session:
            for table in ("candidate_condition_verifications", "candidate_condition_commands"):
                assert (
                    await session.scalar(text("SELECT to_regclass(:name)"), {"name": table}) is None
                )
        await asyncio.to_thread(command.upgrade, requests.config(), "head")
        await asyncio.to_thread(command.check, requests.config())
    finally:
        await asyncio.to_thread(command.upgrade, requests.config(), "head")


@pytest.mark.parametrize(
    "invalid", ["role", "permission", "operation", "route", "run", "job", "body_hash", "subject"]
)
async def test_0077_refusal_requires_precise_trusted_admission(database_session_factory, invalid):
    with pytest.raises(DBAPIError, match="精确原始正文或同源可信准入"):
        await insert_refusal(database_session_factory, invalid=invalid)


@pytest.mark.parametrize("token", ["3.0", "3e0", '"3"', "true", "1"])
async def test_0077_version_is_strict_integer(database_session_factory, token):
    raw = encoded(canonical_command()).replace(
        '"expected_incident_version":3', '"expected_incident_version":' + token
    )
    with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
        await insert_refusal(database_session_factory, raw=raw)


async def test_0077_nested_sources_and_complete_raw_command_contract(database_session_factory):
    original = canonical_command()
    item = original["items"][0]
    invalid_sources = [
        [
            {
                "kind": "human_observation",
                "text": "越界时间",
                "observed_at": "9999-12-31T23:59:59-15:00",
            }
        ],
        [
            {
                "kind": "human_observation",
                "text": "越界时间",
                "observed_at": "0001-01-01T00:00:00+15:00",
            }
        ],
        [{"kind": "evidence", "evidence_id": "ev-1", "text": "注入"}],
        [{"kind": "evidence", "evidence_id": "invalid id"}],
        [{"kind": "human_observation", "text": "观察", "observed_at": "2026-01-01T00:00:00"}],
        [{"kind": "human_observation", "text": "", "observed_at": "2026-01-01T00:00:00Z"}],
        [{"kind": "preflight", "preflight_id": "invalid", "content_sha256": "a" * 64}],
        [{"kind": "evidence", "evidence_id": "ev-1"}] * 2,
        [{"kind": "tool", "sql": "SELECT 1"}],
    ]
    changed_commands = [
        {**original, "items": [{**item, "sources": sources}]} for sources in invalid_sources
    ] + [
        {**original, "items": [{**item, "status": "verified"}]},
        {**original, "items": [{**item, "status": " unresolved "}]},
        {**original, "items": [item, item]},
        {**original, "previous_content_sha256": "a" * 64},
        {**original, "expected_condition_version": 8},
        {**original, "items": [{**item, "unexpected": True}]},
        {**original, "reason": "篡改", "execution_eligible": True},
    ]
    changed_commands.append(
        {
            **original,
            "items": [
                {
                    **item,
                    "condition_id": f"{number:064x}",
                    "sources": [
                        {
                            "kind": "preflight",
                            "preflight_id": str(uuid4()),
                            "content_sha256": "a" * 64,
                        }
                    ],
                }
                for number in range(4)
            ],
        }
    )
    changed_commands.append(
        {
            **original,
            "items": [
                {
                    **item,
                    "condition_id": f"{number:064x}",
                    "sources": [
                        {
                            "kind": "human_observation",
                            "text": str(source_number) + "文" * 450,
                            "observed_at": "2026-01-01T00:00:00Z",
                        }
                        for source_number in range(3)
                    ],
                }
                for number in range(20)
            ],
        }
    )
    for body in changed_commands:
        with pytest.raises(DBAPIError):
            await insert_refusal(database_session_factory, canonical=body)
    # Unicode 裁剪、UUID 文本与时区统一必须与应用的规范命令一致。
    from opsswarm.execution.conditions.contracts import ConditionCommand

    raw = {
        **original,
        "decision_id": "{" + original["decision_id"].upper() + "}",
        "reason": "\u2003工程师核验\u2003",
        "items": [
            {
                **item,
                "sources": [
                    {
                        "kind": "human_observation",
                        "text": "  人工观察  ",
                        "observed_at": "2026-01-01T08:00:00.123400+08:00",
                    }
                ],
            }
        ],
    }
    canonical = ConditionCommand.model_validate(raw).model_dump(mode="json")
    await insert_refusal(database_session_factory, raw=raw, canonical=canonical)
    # API 的严格整数没有人为 int32 上限；不存在资源的拒绝仍须可审计。
    await insert_refusal(
        database_session_factory,
        canonical={
            **original,
            "expected_incident_version": 2**50,
        },
    )
    # 同主体同键仍须逐字绑定此次可信正文。
    with pytest.raises(DBAPIError, match="原始正文与规范命令不一致"):
        await insert_refusal(
            database_session_factory,
            raw=original,
            canonical={**original, "reason": "替换未经授权的理由"},
        )


async def test_0077_error_audit_immutable_and_cannot_downgrade(database_session_factory):
    await insert_refusal(database_session_factory)
    for sql in (
        "UPDATE candidate_condition_commands SET error_message='篡改'",
        "DELETE FROM candidate_condition_commands",
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with database_session_factory() as session, session.begin():
                await session.execute(text(sql))
    with pytest.raises(Exception, match="不能删除已保存的候选条件核验事实"):
        await asyncio.to_thread(command.downgrade, requests.config(), "0076_candidate_preflight")


async def ready_condition(c, *, kind="terminal_handoff", use_preflight=False):
    from tests.integration import test_candidate_decision_migration as decisions

    if use_preflight:
        preflight = await preflights.ready_preflight(c)
        await preflights.apply_preflight(c.db, preflight)
        async with c.db() as session:
            expires = await session.scalar(
                text(
                    "SELECT r.expires_at FROM candidate_approval_requests r "
                    "JOIN candidate_approval_decisions d ON d.request_id=r.id WHERE d.id=:id"
                ),
                {"id": preflight["decision"]},
            )
        approved = {
            "id": preflight["decision"],
            "content_hash": preflight["decision_hash"],
            "plan_hash": c.plan_record["content_sha256"],
            "run": preflight["run"],
            "job": preflight["job"],
            "incident": preflight["incident"],
            "expires": expires,
        }
        c.condition_preflight = preflight
    else:
        approved = await decisions.ready_decision(c, kind=kind)
        await decisions.apply(c.db, approved)
    catalog = []
    for item_kind, field in (
        ("precondition", "preconditions"),
        ("question", "unresolved_questions"),
    ):
        for position, original in enumerate(c.plan_record["content"][field], start=1):
            text_hash = digest(original)
            identity = {
                "plan_id": c.plan_record["id"],
                "plan_content_sha256": c.plan_record["content_sha256"],
                "kind": item_kind,
                "position": position,
                "text_sha256": text_hash,
            }
            catalog.append(
                {
                    "condition_id": digest(encoded(identity)),
                    "kind": item_kind,
                    "position": position,
                    "text": original,
                    "text_sha256": text_hash,
                }
            )
    items = [
        {
            "condition": entry,
            "status": "verified",
            "explanation": "人工已核验原条目",
            "sources": [
                {
                    "kind": "human_observation",
                    "text": "隔离演练环境人工确认",
                    "observed_at": c.clock.current.isoformat().replace("+00:00", "Z"),
                }
            ],
        }
        for entry in catalog
    ]
    content = {
        "version": "candidate-condition-verification-content-v1",
        "decision_id": str(approved["id"]),
        "decision_content_sha256": approved["content_hash"],
        "plan_id": c.plan_record["id"],
        "plan_content_sha256": approved["plan_hash"],
        "items": items,
        "status": "ready_for_review",
        "approval_required": True,
        "execution_eligible": False,
    }
    body = {
        **canonical_command(),
        "decision_id": str(approved["id"]),
        "decision_content_sha256": approved["content_hash"],
        "expected_incident_version": approved["incident"],
        "items": [
            {
                "condition_id": item["condition"]["condition_id"],
                **{k: item[k] for k in ("status", "explanation", "sources")},
            }
            for item in items
        ],
    }
    return {
        "id": uuid5(approved["id"], "candidate-condition-verification:v1:1"),
        "decision": approved["id"],
        "decision_hash": approved["content_hash"],
        "run": approved["run"],
        "job": approved["job"],
        "subject": "migration-engineer",
        "authorization": uuid4(),
        "version": 1,
        "previous": None,
        "previous_hash": None,
        "key": body["idempotency_key"],
        "command": encoded(body),
        "raw": encoded(body),
        "command_hash": digest(encoded(body)),
        "content": encoded(content),
        "content_hash": digest(encoded(content)),
        "incident": approved["incident"],
        "now": c.clock.current,
        "expires": min(approved["expires"], c.clock.current + timedelta(seconds=300)),
    }


async def insert_verification(session, values):
    await session.execute(
        text(
            "INSERT INTO candidate_condition_verifications (id,investigation_run_id,"
            "candidate_job_id,"
            "decision_id,decision_content_sha256,version,previous_id,previous_content_sha256,"
            "incident_version,policy_version,verified_by_id,authorization_id,idempotency_key,"
            "command_sha256,content,content_sha256,created_at,expires_at) VALUES (:id,:run,:job,"
            ":decision,:decision_hash,:version,:previous,:previous_hash,:incident,"
            "'candidate-condition-verification-v1',:subject,:authorization,:key,:command_hash,"
            "CAST(:content AS jsonb),:content_hash,:now,:expires)"
        ),
        values,
    )


async def apply(factory, values, *, audit=True):
    async with factory() as session, session.begin():
        await session.execute(text("SET LOCAL lock_timeout='1s'"))
        await authorize(session, values)
        await insert_verification(session, values)
        if audit:
            await insert_audit(session, values, outcome="applied")


def revised(values, *, version=2, status="blocked"):
    body = json.loads(values["command"])
    content = json.loads(values["content"])
    body.update(
        expected_condition_version=version - 1,
        previous_content_sha256=values["content_hash"],
        idempotency_key=f"native-condition-version-{version}",
    )
    item = {**body["items"][0], "status": "unresolved" if status == "blocked" else "not_applicable"}
    body["items"][0] = item
    content["items"][0].update(status=item["status"])
    content["status"] = status
    return {
        **values,
        "id": uuid5(values["decision"], f"candidate-condition-verification:v1:{version}"),
        "authorization": uuid4(),
        "version": version,
        "previous": values["id"],
        "previous_hash": values["content_hash"],
        "key": body["idempotency_key"],
        "command": encoded(body),
        "raw": encoded(body),
        "command_hash": digest(encoded(body)),
        "content": encoded(content),
        "content_hash": digest(encoded(content)),
    }


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0077_native_content_versions_atomicity_and_sources(wait_context):
    c = wait_context
    values = await ready_condition(c)
    for budget_status in ("reserved", "unknown"):
        with pytest.raises(DBAPIError, match="累计额度|累计模型预算"):
            async with c.db() as session, session.begin():
                await session.execute(text("SET LOCAL session_replication_role=replica"))
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
                        "last_slot": 40 if budget_status == "reserved" else 59,
                        "status": budget_status,
                        "error": None if budget_status == "reserved" else "operation_failed",
                        "finished": None if budget_status == "reserved" else values["now"],
                    },
                )
                await session.execute(text("SET LOCAL session_replication_role=origin"))
                attacked = {**values, "authorization": uuid4()}
                await authorize(session, attacked)
                await insert_verification(session, attacked)
    for field, changed in (
        ("decision_hash", "b" * 64),
        ("incident", values["incident"] + 1),
        ("expires", values["expires"] + timedelta(seconds=1)),
        ("content_hash", "b" * 64),
        ("version", 2),
    ):
        with pytest.raises(DBAPIError):
            await apply(c.db, {**values, field: changed, "authorization": uuid4()})
    original = json.loads(values["content"])
    for change in (
        {"execution_eligible": True},
        {"approval_required": False},
        {"status": "blocked"},
        {"items": []},
        {"plan_id": str(uuid4())},
        {
            "items": [
                {
                    **original["items"][0],
                    "condition": {**original["items"][0]["condition"], "text": "改写原文"},
                }
            ]
        },
        {
            "items": [
                {
                    **original["items"][0],
                    "sources": [{"kind": "evidence", "evidence_id": "foreign"}],
                }
            ]
        },
    ):
        content = encoded({**original, **change})
        with pytest.raises(DBAPIError):
            await apply(
                c.db,
                {
                    **values,
                    "authorization": uuid4(),
                    "content": content,
                    "content_hash": digest(content),
                },
            )
    with pytest.raises(DBAPIError, match="同事务精确成功命令闭环"):
        await apply(c.db, values, audit=False)
    await apply(c.db, values)
    second = revised(values)
    # 前驱不能缺失或偷换散列；攻击失败不留下中间孤儿。
    for changed in ({"previous": uuid4()}, {"previous_hash": "c" * 64}, {"version": 3}):
        with pytest.raises(DBAPIError):
            await apply(c.db, {**second, **changed, "authorization": uuid4()})
    await apply(c.db, second)
    third = revised(second, version=3, status="plan_revision_required")
    await apply(c.db, third)
    from opsswarm.execution.conditions.store import CandidateConditionStore

    store = CandidateConditionStore(c.db, clock=c.clock)
    state = await store.get_state(values["run"], values["job"])
    assert state.condition_version == 3 and state.latest.id == third["id"]
    assert state.availability == "current"
    historical = await store.get_version(values["run"], values["job"], 1)
    assert historical.record.id == values["id"] and historical.availability == "superseded"
    async with c.db() as session, session.begin():
        replay = {**values, "authorization": uuid4()}
        await authorize(session, replay)
        await insert_audit(session, replay, outcome="replayed")
    with pytest.raises(DBAPIError, match="不能降级"):
        async with c.db() as session, session.begin():
            refused = {**values, "authorization": uuid4()}
            await authorize(session, refused)
            await insert_audit(session, refused)
    for sql in (
        "UPDATE candidate_condition_verifications SET content='{}'::jsonb",
        "DELETE FROM candidate_condition_verifications",
    ):
        with pytest.raises(DBAPIError, match="不可更新或删除"):
            async with c.db() as session, session.begin():
                await session.execute(text(sql))
    with pytest.raises(Exception, match="不能删除已保存的候选条件核验事实"):
        await asyncio.to_thread(command.downgrade, requests.config(), "0076_candidate_preflight")


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_0077_native_preflight_sources_and_same_transaction_prefix(wait_context):
    c = wait_context
    values = await ready_condition(c, use_preflight=True)
    preflight = c.condition_preflight
    content = json.loads(values["content"])
    body = json.loads(values["command"])
    evidence = c.plan_record["content"]["evidence_ids"][0]
    sources = [
        {
            "kind": "preflight",
            "preflight_id": str(preflight["id"]),
            "content_sha256": preflight["content_hash"],
        },
        {"kind": "evidence", "evidence_id": evidence},
    ]
    for item in content["items"]:
        item["sources"] = sources
    for item in body["items"]:
        item["sources"] = sources
    values.update(
        content=encoded(content),
        content_hash=digest(encoded(content)),
        command=encoded(body),
        raw=encoded(body),
        command_hash=digest(encoded(body)),
        expires=preflight["expires"],
    )
    # 真实预检必须绑定同一决定与摘要，错误观测也不能作为核验依据。
    for field, value in (("preflight_id", str(uuid4())), ("content_sha256", "b" * 64)):
        attacked_content = json.loads(values["content"])
        attacked_content["items"][0]["sources"][0][field] = value
        encoded_content = encoded(attacked_content)
        with pytest.raises(DBAPIError):
            await apply(
                c.db,
                {
                    **values,
                    "authorization": uuid4(),
                    "content": encoded_content,
                    "content_hash": digest(encoded_content),
                },
            )
    second = revised(values)
    # 原生同事务允许完整连续前缀，延迟闭环在最终可见状态校验所有版本。
    async with c.db() as session, session.begin():
        await authorize(session, values)
        await insert_verification(session, values)
        await insert_audit(session, values, outcome="applied")
        await authorize(session, second)
        await insert_verification(session, second)
        await insert_audit(session, second, outcome="applied")
    from opsswarm.execution.conditions.store import CandidateConditionStore

    state = await CandidateConditionStore(c.db, clock=c.clock).get_state(
        values["run"], values["job"]
    )
    assert state.condition_version == 2 and state.latest.id == second["id"]
    # 锁定旧工程师授权不能让历史重放反向等待；只锁此次新授权。
    async with c.db() as holder, holder.begin():
        await holder.execute(
            text("SELECT id FROM operator_authorizations WHERE id=:id FOR UPDATE"),
            {"id": values["authorization"]},
        )
        replay = {**values, "authorization": uuid4()}
        async with c.db() as session, session.begin():
            await session.execute(text("SET LOCAL lock_timeout='1s'"))
            await authorize(session, replay)
            await insert_audit(session, replay, outcome="replayed")
    # 最高版本发生带外损坏后，旧版重放也不能回退接受。
    async with c.db() as session, session.begin():
        await session.execute(text("SET LOCAL session_replication_role=replica"))
        await session.execute(
            text("UPDATE candidate_condition_verifications SET content_sha256=:hash WHERE id=:id"),
            {"hash": "c" * 64, "id": second["id"]},
        )
        await session.execute(text("SET LOCAL session_replication_role=origin"))
    with pytest.raises(DBAPIError, match="摘要|审批执行资格"):
        async with c.db() as session, session.begin():
            replay = {**values, "authorization": uuid4()}
            await authorize(session, replay)
            await insert_audit(session, replay, outcome="replayed")
