"""真实候选进入独立草案，验证可信准入、来源隔离和回滚。"""

import asyncio
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import event, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from opsswarm.identity.tables import OperatorAuthorizationRow
from opsswarm.incidents.persistence.tables import IncidentRow
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.candidate.output import CandidateModelResult, CandidateProposal
from opsswarm.investigation.candidate.tables import CandidateJobRow
from opsswarm.planning.candidate.store import CandidatePlanStore
from opsswarm.planning.candidate.tables import CandidatePlanCommandRow, CandidatePlanRow
from tests.api.test_candidate_evaluation_api import (
    context,
    evaluate,
    get_candidate,
    unsupported,
    worker,
)
from tests.api.test_human_reassessment_api import RecordingModel, count
from tests.api.test_human_wait_api import command, post
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.identity_helpers import auth_header

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


async def ready(c, *, kind="terminal_handoff", supported=True, run_worker=True):
    await context(c, kind)
    created = await evaluate(c)
    assert created.status_code == 201, created.text
    c.job_id = UUID(created.json()["job"]["id"])
    c.plan_base = f"{c.candidate_base}/{c.job_id}/action-plan"
    c.app.state.candidate_plan_store = CandidatePlanStore(c.db, clock=c.clock)
    async with c.db() as session:
        job = await session.get(CandidateJobRow, c.job_id)
        evidence = [item["evidence_id"] for item in job.source["evidence"]]
        incident = await session.get(IncidentRow, c.started.run.incident_id)
        c.incident_version = incident.version
    if run_worker:
        result = (
            CandidateModelResult(
                proposal=CandidateProposal(
                    status="candidate",
                    action_type="rollback_release",
                    rationale="核验变更版本后的待审候选",
                    evidence_ids=evidence,
                    preconditions=("规划后另行可信审批",),
                    unresolved_questions=("执行前仍需确认当前运行版本",),
                )
            )
            if supported
            else unsupported()
        )
        assert await worker(c, RecordingModel(result=result)).run_once()
    view = (await get_candidate(c, c.job_id)).json()
    c.plan_body = dict(
        action="create_plan",
        candidate_result_sha256=view["result"]["content_sha256"] if view["result"] else "0" * 64,
        expected_wait_version=view["wait_version"],
        expected_incident_version=c.incident_version,
        idempotency_key="candidate-plan-command-117",
        reason="工程师请求生成待审草案",
    )
    return view


async def create(c, body=None, *, header=None):
    return await c.client.post(
        c.plan_base, json=body or c.plan_body, headers=header or auth_header("engineer")
    )


async def get(c):
    return await c.client.get(c.plan_base, headers=auth_header("viewer"))


@pytest.mark.parametrize(
    ("wait_context", "kind"),
    [("candidate_with_change", "terminal_handoff"), ("human_clean_with_change", "human_route")],
    indirect=["wait_context"],
)
async def test_two_sources_create_bound_draft_without_budget_or_old_authorization(
    wait_context, kind
):
    c = wait_context
    candidate = await ready(c, kind=kind)
    before = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    old_run = await c.store.get_run(c.started.run.id)
    old_work = await c.store.get_work_item(c.started.work_item.id)
    assert (await get(c)).json() == {"record": None, "availability": "pending"}
    response = await create(c)
    assert response.status_code == 201, response.text
    record = response.json()["plan"]["record"]
    content = record["content"]
    assert content["plan"]["status"] == "draft" and content["plan"]["version"] == 1
    assert content["plan"]["steps"] == [candidate["result"]["content"]["step"]]
    assert content["preconditions"] == candidate["result"]["content"]["proposal"]["preconditions"]
    assert content["unresolved_questions"] == ["执行前仍需确认当前运行版本"]
    assert content["approval_eligible"] is False and content["execution_eligible"] is False
    assert "root_cause" not in content and "hypothesis_id" not in record
    assert (await get(c)).json() == response.json()["plan"]
    assert (await create(c)).status_code == 200
    assert await CallBudgetQueries(c.db).get_snapshot(c.started.run.id) == before
    assert await c.store.get_run(c.started.run.id) == old_run
    assert await c.store.get_work_item(c.started.work_item.id) == old_work
    async with c.db() as session:
        for table in ("action_plans", "action_steps", "approval_requests", "operation_executions"):
            assert await session.scalar(text(f"SELECT count(*) FROM {table}")) == 0
    plan_id = record["id"]
    # 新草案ID不属于旧计划表，无法通过旧审批/执行路由借用授权。
    result = await c.client.post(
        f"/api/v1/action-plans/{plan_id}/approval-requests",
        json={"expected_incident_version": c.incident_version, "reason": "拒绝借用旧审批"},
        headers=auth_header("engineer"),
    )
    assert result.status_code == 404, result.text
    result = await c.client.post(
        f"/api/v1/action-steps/{uuid4()}/executions",
        json={
            "expected_incident_version": c.incident_version,
            "idempotency_key": "reject-candidate-execution",
        },
        headers=auth_header("executor"),
    )
    assert result.status_code == 404, result.text
    assert await count(c, CandidatePlanRow) == 1


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_trust_concurrent_idempotency_full_body_conflicts_and_audits(wait_context):
    c = wait_context
    await ready(c)
    assert (await c.client.post(c.plan_base, json=c.plan_body)).status_code == 401
    for role in ("viewer", "approver", "executor", "alert_source"):
        assert (await create(c, header=auth_header(role))).status_code == 403
    responses = await asyncio.gather(create(c), create(c))
    assert sorted(r.status_code for r in responses) == [200, 201]
    for change in (
        {"reason": "另一命令"},
        {"candidate_result_sha256": "f" * 64},
        {"idempotency_key": "another-plan-key"},
    ):
        response = await create(c, {**c.plan_body, **change})
        assert response.status_code == 409, response.text
    body = dict(c.plan_body)
    body.pop("action")
    for key in ("reason", "idempotency_key", "candidate_result_sha256"):
        body[key] = " \n\t\u3000" + body[key] + " \n\t\u3000"
    assert (await create(c, body)).status_code == 200
    audits = await c.client.get(c.plan_base + "/commands", headers=auth_header("viewer"))
    assert audits.status_code == 200, audits.text
    assert [i["outcome"] for i in audits.json()["items"]].count("error") == 3
    assert (await c.client.get(c.plan_base)).status_code == 401
    assert await count(c, CandidatePlanRow) == 1


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_hash_wait_and_incident_version_failures_are_audited(wait_context):
    c = wait_context
    await ready(c)
    for index, change in enumerate(
        (
            {"candidate_result_sha256": "a" * 64},
            {"expected_wait_version": 2},
            {"expected_incident_version": c.incident_version + 1},
        )
    ):
        response = await create(
            c, {**c.plan_body, "idempotency_key": f"invalid-plan-key-{index}", **change}
        )
        assert response.status_code == 409, response.text
    assert await count(c, CandidatePlanRow) == 0
    assert await count(c, CandidatePlanCommandRow) == 3
    response = await c.client.post(
        c.plan_base.replace(str(c.job_id), str(uuid4())),
        json=c.plan_body,
        headers=auth_header("engineer"),
    )
    assert response.status_code == 404, response.text


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("run_worker", [True, False])
async def test_unsupported_or_unfinished_candidate_never_becomes_plan(wait_context, run_worker):
    c = wait_context
    await ready(c, supported=False, run_worker=run_worker)
    response = await create(c)
    assert response.status_code == 409, response.text
    assert await count(c, CandidatePlanRow) == 0


@pytest.mark.parametrize("wait_context", ["human_clean_with_change"], indirect=True)
@pytest.mark.parametrize("action", ["supplement", "cancel"])
async def test_human_change_preserves_history_and_blocks_new_admission(wait_context, action):
    c = wait_context
    await ready(c, kind="human_route")
    response = await create(c)
    assert response.status_code == 201, response.text
    saved = response.json()["plan"]["record"]
    assert (
        await post(c, command(c, action=action, version=2, key="changed-plan-source-117"))
    ).status_code == 201
    view = (await get(c)).json()
    assert view["availability"] == "stale" and view["record"] == saved
    assert (await create(c)).status_code == 200
    assert (
        await create(c, {**c.plan_body, "idempotency_key": "new-after-source-change"})
    ).status_code == 409


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("phase", ["before", "after_flush", "auth_after_flush"])
async def test_deadline_or_auth_expiry_never_leaves_partial_plan(wait_context, phase):
    c = wait_context
    await ready(c)
    if phase == "before":
        c.clock.current += timedelta(seconds=901)
    advanced = False

    def expire(session, flush_context):
        nonlocal advanced
        if (
            phase != "before"
            and not advanced
            and any(isinstance(row, CandidatePlanCommandRow) for row in session.new)
        ):
            advanced = True
            c.clock.current += (
                timedelta(days=2) if phase == "auth_after_flush" else timedelta(seconds=901)
            )

    event.listen(Session, "after_flush", expire)
    try:
        response = await create(c)
    finally:
        event.remove(Session, "after_flush", expire)
    assert response.status_code == (401 if phase == "auth_after_flush" else 409), response.text
    assert await count(c, CandidatePlanRow) == 0
    assert await count(c, CandidatePlanCommandRow) == (1 if phase == "before" else 0)


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_native_immutable_content_and_plan_isolation(wait_context):
    c = wait_context
    await ready(c)
    response = await create(c)
    assert response.status_code == 201, response.text
    plan_id = UUID(response.json()["plan"]["record"]["id"])
    async with c.db() as session:
        with pytest.raises(DBAPIError):
            await session.execute(
                text("UPDATE candidate_action_plans SET content_sha256=:hash WHERE id=:id"),
                {"hash": "0" * 64, "id": plan_id},
            )
            await session.commit()
    assert (await get(c)).status_code == 200


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
@pytest.mark.parametrize("change", ["version", "deadline"])
async def test_plan_currentness_includes_incident_version_and_original_deadline(
    wait_context, change
):
    c = wait_context
    await ready(c)
    response = await create(c)
    assert response.status_code == 201, response.text
    saved = response.json()["plan"]["record"]
    if change == "version":
        async with c.db() as session, session.begin():
            await session.execute(
                text("UPDATE incidents SET version=version+1 WHERE id=:id"),
                {"id": c.started.run.incident_id},
            )
    else:
        c.clock.current += timedelta(seconds=901)
    view = (await get(c)).json()
    assert view["availability"] == "stale" and view["record"] == saved
    assert (await create(c)).status_code == 200
    assert (
        await create(c, {**c.plan_body, "idempotency_key": "another-stale-plan-117"})
    ).status_code == 409


@pytest.mark.parametrize("wait_context", ["candidate_with_change"], indirect=True)
async def test_auth_expired_after_authorization_lock_preserves_401_and_rolls_back(
    wait_context, monkeypatch
):
    c = wait_context
    await ready(c)
    original = AsyncSession.get
    advanced = False

    async def acquire(self, entity, *args, **kwargs):
        nonlocal advanced
        row = await original(self, entity, *args, **kwargs)
        if entity is OperatorAuthorizationRow and kwargs.get("with_for_update") and not advanced:
            advanced = True
            c.clock.current += timedelta(days=2)
        return row

    monkeypatch.setattr(AsyncSession, "get", acquire)
    response = await create(c)
    assert advanced and response.status_code == 401, response.text
    assert response.json()["error_code"] == "operator_unauthenticated"
    assert await count(c, CandidatePlanRow) == 0
    assert await count(c, CandidatePlanCommandRow) == 0
