"""真实批准下的版本化条件、可信核验与不可执行历史。"""

import asyncio
import copy
import hashlib
import json
from datetime import datetime, timedelta
from uuid import UUID, uuid4, uuid5

import pytest
from sqlalchemy import event, text
from sqlalchemy.orm import Session

from opsswarm.identity.tables import OperatorAuthorizationRow
from tests.api.test_candidate_approval_api import get as get_request
from tests.api.test_candidate_approval_api import incident_events
from tests.api.test_candidate_decision_api import get as get_decision
from tests.api.test_candidate_decision_api import parent_facts
from tests.api.test_candidate_plan_api import get as get_plan
from tests.api.test_candidate_preflight_api import create as create_preflight
from tests.api.test_candidate_preflight_api import get as get_preflights
from tests.api.test_candidate_preflight_api import ready as ready_preflight
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.identity_helpers import auth_header, token

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def canonical_hash(value):
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
    ).hexdigest()


async def ready(c, *, kind="terminal_handoff"):
    from opsswarm.execution.conditions.store import CandidateConditionStore

    await ready_preflight(c, kind=kind)
    c.app.state.candidate_condition_store = CandidateConditionStore(c.db, clock=c.clock)
    c.conditions_base = f"{c.candidate_base}/{c.job_id}/execution-conditions"
    response = await get(c)
    assert response.status_code == 200, response.text
    c.catalog = response.json()["catalog"]
    c.conditions_body = dict(
        decision_id=c.decision_record["id"],
        decision_content_sha256=c.decision_record["content_sha256"],
        expected_incident_version=c.decision_record["incident_version"],
        expected_condition_version=0,
        previous_content_sha256=None,
        items=[
            dict(
                condition_id=item["condition_id"],
                status="verified",
                explanation="工程师已核验本条约束，当前判断仅供新审批审查",
                sources=[
                    dict(
                        kind="human_observation",
                        text="人工观察并确认原条目；该陈述不替代系统遥测或执行授权",
                        observed_at=c.clock().isoformat(),
                    )
                ],
            )
            for item in c.catalog
        ],
        idempotency_key="candidate-conditions-command-121",
        reason="保存完整可信核验快照，交给未来独立审批",
    )


async def create(c, body=None, *, header=None):
    return await c.client.post(
        c.conditions_base,
        json=c.conditions_body if body is None else body,
        headers=header or auth_header("engineer"),
    )


async def get(c, suffix=""):
    return await c.client.get(c.conditions_base + suffix, headers=auth_header("viewer"))


def following(c, record, *, status="verified", key=None):
    body = copy.deepcopy(c.conditions_body)
    body.update(
        expected_condition_version=record["version"],
        previous_content_sha256=record["content_sha256"],
        idempotency_key=key or f"condition-revision-{record['version'] + 1}-121",
    )
    body["items"][0]["status"] = status
    return body


async def fact_counts(c):
    async with c.db() as session:
        return tuple(
            [
                await session.scalar(text(f"SELECT count(*) FROM {table}"))
                for table in ("candidate_condition_verifications", "candidate_condition_commands")
            ]
        )


async def replace(c, sql, values):
    # 仅隔离测试库模拟落盘损坏，业务入口始终受到不可变守卫保护。
    async with c.db() as session, session.begin():
        await session.execute(text("SET LOCAL session_replication_role = replica"))
        await session.execute(text(sql), values)
        await session.execute(text("SET LOCAL session_replication_role = origin"))


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_keep_exact_catalog_three_states_and_old_facts(wait_context, kind):
    c = wait_context
    await ready(c, kind=kind)
    preflight = await create_preflight(c)
    assert preflight.status_code == 201, preflight.text
    before_parent, before_events = await parent_facts(c), await incident_events(c)
    catalog = c.catalog
    expected = [
        (kind, position, value)
        for kind, name in (("precondition", "preconditions"), ("question", "unresolved_questions"))
        for position, value in enumerate(c.plan_record["content"][name], 1)
    ]
    assert len(catalog) == len(expected)
    for item, (item_kind, position, value) in zip(catalog, expected, strict=True):
        text_hash = hashlib.sha256(value.encode("utf-8")).hexdigest()
        assert item == dict(
            condition_id=canonical_hash(
                dict(
                    plan_id=c.plan_record["id"],
                    plan_content_sha256=c.plan_record["content_sha256"],
                    kind=item_kind,
                    position=position,
                    text_sha256=text_hash,
                )
            ),
            kind=item_kind,
            position=position,
            text=value,
            text_sha256=text_hash,
        )
    assert (await get(c)).json() == dict(
        catalog=catalog,
        condition_version=0,
        latest=None,
        availability="pending",
        approval_required=True,
        execution_eligible=False,
    )
    first = await create(c)
    assert first.status_code == 201, first.text
    record = first.json()["verification"]["record"]
    assert first.json()["applied"] is True
    assert record["version"] == 1 and record["previous_id"] is None
    assert record["id"] == str(
        uuid5(UUID(c.decision_record["id"]), "candidate-condition-verification:v1:1")
    )
    assert record["verified_by_id"] == "trusted-engineer"
    assert record["content"]["status"] == "ready_for_review"
    assert record["content_sha256"] == canonical_hash(record["content"])
    assert [item["condition"] for item in record["content"]["items"]] == catalog
    assert datetime.fromisoformat(record["expires_at"]) == min(
        datetime.fromisoformat(c.request_record["expires_at"]), c.clock() + timedelta(seconds=300)
    )
    async with c.db() as session:
        admission = await session.get(OperatorAuthorizationRow, UUID(record["authorization_id"]))
        assert admission.role == "engineer" and admission.operation == "verify_candidate_conditions"
        assert admission.resources == {"run_id": str(c.started.run.id), "job_id": str(c.job_id)}
    for status, outcome in (
        ("unresolved", "blocked"),
        ("not_applicable", "plan_revision_required"),
    ):
        body = following(c, record, status=status)
        response = await create(c, body)
        assert response.status_code == 201, response.text
        newer = response.json()["verification"]["record"]
        assert newer["previous_id"] == record["id"]
        assert newer["previous_content_sha256"] == record["content_sha256"]
        assert newer["version"] == record["version"] + 1
        assert newer["content"]["status"] == outcome
        assert newer["content"]["approval_required"] is True
        assert newer["content"]["execution_eligible"] is False
        assert (await get(c, f"/versions/{record['version']}")).json() == {
            "record": record,
            "availability": "superseded",
        }
        record = newer
    state = (await get(c)).json()
    assert state["catalog"] == catalog and state["condition_version"] == 3
    assert state["latest"] == record and state["availability"] == "current"
    replay = await create(c)
    assert replay.status_code == 200 and replay.json()["applied"] is False
    assert replay.json()["verification"]["availability"] == "superseded"
    page = (await get(c, "/commands?limit=2")).json()
    assert page["next_cursor"] is not None
    rest = (await get(c, f"/commands?after_cursor={page['next_cursor']}")).json()
    assert [item["outcome"] for item in page["items"] + rest["items"]] == [
        "applied",
        "applied",
        "applied",
        "replayed",
    ]
    assert (await get_plan(c)).json()["record"] == c.plan_record
    assert (await get_request(c)).json()["record"] == c.request_record
    assert (await get_decision(c)).json()["record"] == c.decision_record
    assert (await get_preflights(c)).json()["items"] == [preflight.json()["record"]]
    assert await parent_facts(c) == before_parent
    assert len(await incident_events(c)) == len(before_events)
    async with c.db() as session:
        for table in ("action_plans", "action_steps", "approval_requests", "operation_executions"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_strict_body_trusted_engineer_and_complete_snapshot_boundaries(wait_context):
    c = wait_context
    await ready(c)
    for path in (
        c.conditions_base,
        c.conditions_base + "/commands",
        c.conditions_base + "/versions/1",
    ):
        assert (await c.client.get(path)).status_code == 401
        assert (await c.client.get(path, headers=auth_header("alert_source"))).status_code == 403
    assert (await c.client.post(c.conditions_base, json=c.conditions_body)).status_code == 401
    for role in ("viewer", "approver", "executor", "alert_source"):
        assert (await create(c, header=auth_header(role))).status_code == 403
    for change in (
        {"actor_id": "forged"},
        {"authorization_id": str(uuid4())},
        {"execution_eligible": True},
        {"approval_required": False},
        {"expected_incident_version": True},
        {"expected_condition_version": "0"},
        {"expected_condition_version": 8},
        {"expected_condition_version": 1},
        {"previous_content_sha256": "a" * 64},
        {"decision_content_sha256": "invalid"},
        {"reason": " \n\t\u3000"},
        {"items": []},
        {"items": [c.conditions_body["items"][0]] * 2},
    ):
        response = await create(c, {**c.conditions_body, **change})
        assert response.status_code == 422, response.text
    for field in c.conditions_body:
        body = copy.deepcopy(c.conditions_body)
        body.pop(field)
        assert (await create(c, body)).status_code == 422
    for change in (
        {"text": "客户端不能改写原条目"},
        {"status": "unverified"},
        {"sources": []},
        {"explanation": " \n"},
        {"sources": [c.conditions_body["items"][0]["sources"][0]] * 2},
        {"sources": [{"kind": "external_url", "url": "http://localhost"}]},
        {
            "sources": [
                {
                    "kind": "human_observation",
                    "text": "观察时间必须带时区",
                    "observed_at": c.clock().replace(tzinfo=None).isoformat(),
                }
            ]
        },
    ):
        body = copy.deepcopy(c.conditions_body)
        body["items"][0].update(change)
        response = await create(c, body)
        assert response.status_code == 422, response.text
    for index, items in enumerate(
        (
            c.conditions_body["items"][:-1],
            list(reversed(c.conditions_body["items"])),
            [
                {**c.conditions_body["items"][0], "condition_id": "f" * 64},
                *c.conditions_body["items"][1:],
            ],
        )
    ):
        response = await create(
            c, {**c.conditions_body, "items": items, "idempotency_key": f"bad-catalog-{index}-121"}
        )
        assert response.status_code == 409, response.text
    oversized = await c.client.post(
        c.conditions_base,
        content=json.dumps(c.conditions_body) + " " * 65536,
        headers={**auth_header("engineer"), "Content-Type": "application/json"},
    )
    assert oversized.status_code == 413
    bounded = copy.deepcopy(c.conditions_body)
    bounded["items"] = [
        {
            "condition_id": f"{index:064x}",
            "status": "verified",
            "explanation": "x" * 500,
            "sources": [
                {
                    "kind": "human_observation",
                    "text": str(source) + "x" * 499,
                    "observed_at": c.clock().isoformat(),
                }
                for source in range(3)
            ],
        }
        for index in range(20)
    ]
    # 每字段合法但整个规范命令过大，须在访问来源之前拒绝。
    assert (await create(c, bounded)).status_code == 422
    c.app.state.operator_auth_settings = c.settings.model_copy(update={"mode": "legacy_local"})
    assert (await c.client.post(c.conditions_base, json=c.conditions_body)).status_code == 401
    c.app.state.operator_auth_settings = c.settings
    assert (await fact_counts(c))[0] == 0
    assert (await create(c)).status_code == 201


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_concurrent_replay_cas_predecessor_and_eight_version_limit(wait_context):
    c = wait_context
    await ready(c)
    responses = await asyncio.gather(create(c), create(c))
    assert sorted(result.status_code for result in responses) == [200, 201]
    assert responses[0].json()["verification"] == responses[1].json()["verification"]
    record = responses[0].json()["verification"]["record"]
    assert (await create(c, {**c.conditions_body, "reason": "同键不同正文"})).status_code == 409
    next_body = following(c, record)
    wrong_hash = {
        **next_body,
        "previous_content_sha256": "f" * 64,
        "idempotency_key": "wrong-predecessor-121",
    }
    assert (await create(c, wrong_hash)).status_code == 409
    competitors = await asyncio.gather(
        create(c, next_body),
        create(c, {**next_body, "idempotency_key": "competing-version-two-121"}),
    )
    assert sorted(result.status_code for result in competitors) == [201, 409]
    record = next(result for result in competitors if result.status_code == 201).json()[
        "verification"
    ]["record"]
    while record["version"] < 8:
        result = await create(c, following(c, record))
        assert result.status_code == 201, result.text
        record = result.json()["verification"]["record"]
    old_cas = following(c, record)
    old_cas.update(expected_condition_version=7, idempotency_key="beyond-eight-121")
    assert (await create(c, old_cas)).status_code == 409
    assert (await get(c)).json()["condition_version"] == 8
    assert (await fact_counts(c))[0] == 8
    other = {"Authorization": f"Bearer engineer-two.{token('engineer').split('.')[1]}"}
    assert (await create(c, header=other)).status_code == 409
    for suffix in ("/versions/0", "/versions/9", "/commands?limit=0", "/commands?limit=101"):
        assert (await get(c, suffix)).status_code == 422
    assert (await get(c, "/versions/8")).json()["record"] == record


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_evidence_human_and_preflight_references_keep_bounded_fresh_sources(wait_context):
    c = wait_context
    await ready(c)
    preflight = await create_preflight(c)
    assert preflight.status_code == 201, preflight.text
    check = preflight.json()["record"]
    base = copy.deepcopy(c.conditions_body)
    base["items"][0]["sources"] = [
        {"kind": "evidence", "evidence_id": c.decision_record["content"]["evidence_ids"][0]}
    ]
    base["items"][1]["sources"] = [
        {
            "kind": "preflight",
            "preflight_id": check["id"],
            "content_sha256": check["content_sha256"],
        }
    ]
    for index, source in enumerate(
        (
            {"kind": "evidence", "evidence_id": "foreign-evidence"},
            {
                "kind": "preflight",
                "preflight_id": str(uuid4()),
                "content_sha256": check["content_sha256"],
            },
            {"kind": "preflight", "preflight_id": check["id"], "content_sha256": "a" * 64},
            {
                "kind": "human_observation",
                "text": "观察时间不能早于批准",
                "observed_at": (c.clock() - timedelta(seconds=1)).isoformat(),
            },
            {
                "kind": "human_observation",
                "text": "不能声称观察未来",
                "observed_at": (c.clock() + timedelta(seconds=1)).isoformat(),
            },
        )
    ):
        body = copy.deepcopy(base)
        body.update(idempotency_key=f"invalid-condition-source-{index}-121")
        body["items"][0]["sources"] = [source]
        result = await create(c, body)
        assert result.status_code == 409, result.text
    result = await create(c, base)
    assert result.status_code == 201, result.text
    record = result.json()["verification"]["record"]
    assert record["expires_at"] == check["expires_at"]
    assert record["content"]["status"] == "ready_for_review"
    assert c.observer.steps and len(c.observer.steps) == 1
    c.clock.current = datetime.fromisoformat(check["expires_at"])
    assert (await get(c)).json()["availability"] == "expired"
    expired_reference = following(c, record)
    expired_reference["items"] = base["items"]
    assert (await create(c, expired_reference)).status_code == 409
    fresh = following(c, record, key="fresh-after-expired-condition-121")
    for item in fresh["items"]:
        item["sources"][0]["observed_at"] = c.clock().isoformat()
    newer = await create(c, fresh)
    assert newer.status_code == 201, newer.text
    assert newer.json()["verification"]["availability"] == "current"
    assert (await get(c, "/versions/1")).json()["availability"] == "superseded"
    assert len(c.observer.steps) == 1


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_currentness_deadline_incident_and_source_drift_do_not_renew_history(
    wait_context, monkeypatch
):
    c = wait_context
    await ready(c)
    first = await create(c)
    assert first.status_code == 201, first.text
    record = first.json()["verification"]["record"]
    before_parent = await parent_facts(c)

    async def unavailable_budget(*args, **kwargs):
        return False

    # 只验证HTTP用例消费预算门禁；真实额度规则由原生及纯规则测试覆盖。
    with monkeypatch.context() as guard:
        guard.setattr(c.app.state.candidate_condition_store, "_budget_current", unavailable_budget)
        assert (await get(c)).json()["availability"] == "stale"
        refused = await create(c, following(c, record, key="budget-gate-denied-121"))
        assert refused.status_code == 409, refused.text
        assert refused.json()["error_code"] == "candidate_condition_budget_not_current"
    assert (await get(c)).json()["availability"] == "current"
    async with c.db() as session, session.begin():
        await session.execute(
            text("UPDATE incidents SET version=version+1 WHERE id=:id"),
            {"id": c.started.run.incident_id},
        )
    state = (await get(c)).json()
    assert state["availability"] == "stale" and state["latest"] == record
    assert (await create(c)).json()["verification"] == {"record": record, "availability": "stale"}
    assert (await create(c, following(c, record))).status_code == 409
    c.clock.current = datetime.fromisoformat(c.request_record["expires_at"])
    assert (await get(c)).json()["availability"] == "expired"
    assert (await create(c)).json()["verification"] == {"record": record, "availability": "expired"}
    assert (await fact_counts(c))[0] == 1
    assert await parent_facts(c) == before_parent
    async with c.db() as session:
        original = await session.scalar(
            text(
                "SELECT source_content_sha256 FROM investigation_candidate_evaluation_jobs "
                "WHERE id=:id"
            ),
            {"id": c.job_id},
        )
    sql = (
        "UPDATE investigation_candidate_evaluation_jobs SET source_content_sha256=:value "
        "WHERE id=:id"
    )
    try:
        await replace(c, sql, {"id": c.job_id, "value": "f" * 64})
        for result in (await get(c), await get(c, "/commands"), await create(c)):
            assert result.status_code == 409, result.text
    finally:
        await replace(c, sql, {"id": c.job_id, "value": original})
    assert (await get(c)).json()["latest"] == record


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("phase", ["deadline", "auth"])
async def test_after_flush_expiry_rolls_back_version_and_applied_audit(wait_context, phase):
    c = wait_context
    await ready(c)
    before_parent, before_events = await parent_facts(c), await incident_events(c)
    advanced = False

    def advance(session, flush_context):
        nonlocal advanced
        if not advanced and any(
            row.__tablename__ == "candidate_condition_commands" and row.outcome == "applied"
            for row in session.new
        ):
            advanced = True
            c.clock.current = (
                datetime.fromisoformat(c.request_record["expires_at"])
                if phase == "deadline"
                else c.clock() + timedelta(days=2)
            )

    event.listen(Session, "after_flush", advance)
    try:
        response = await create(c)
    finally:
        event.remove(Session, "after_flush", advance)
    assert advanced and response.status_code == (409 if phase == "deadline" else 401), response.text
    assert await fact_counts(c) == (0, 0)
    assert await parent_facts(c) == before_parent
    assert len(await incident_events(c)) == len(before_events)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_corrupt_highest_version_never_falls_back_or_downgrades_replay(wait_context):
    c = wait_context
    await ready(c)
    first = await create(c)
    assert first.status_code == 201, first.text
    original_record = first.json()["verification"]["record"]
    second_body = following(c, original_record)
    second = await create(c, second_body)
    assert second.status_code == 201, second.text
    record = second.json()["verification"]["record"]
    before = await fact_counts(c)
    content_sql = (
        "UPDATE candidate_condition_verifications SET content=CAST(:value AS jsonb) WHERE id=:id"
    )
    original_content = json.dumps(record["content"], ensure_ascii=False)
    attacks = (
        (
            "UPDATE candidate_condition_verifications SET content_sha256=:value WHERE id=:id",
            "f" * 64,
            record["content_sha256"],
        ),
        (
            content_sql,
            json.dumps({**record["content"], "execution_eligible": 0}, ensure_ascii=False),
            original_content,
        ),
        (
            content_sql,
            json.dumps({**record["content"], "execution_eligible": True}, ensure_ascii=False),
            original_content,
        ),
    )
    for sql, corrupt, original in attacks:
        try:
            await replace(c, sql, {"id": UUID(record["id"]), "value": corrupt})
            for result in (
                await get(c),
                await get(c, "/versions/1"),
                await get(c, "/versions/2"),
                await get(c, "/commands"),
                await create(c),
                await create(c, second_body),
            ):
                assert result.status_code == 409, result.text
            assert await fact_counts(c) == before
        finally:
            await replace(c, sql, {"id": UUID(record["id"]), "value": original})
        assert (await get(c)).json()["latest"] == record
    replay = await create(c)
    assert replay.status_code == 200, replay.text
    assert replay.json()["verification"] == {
        "record": original_record,
        "availability": "superseded",
    }
