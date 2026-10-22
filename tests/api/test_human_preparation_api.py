"""真实人工交接到独立准备 Worker 的可信、预算及恢复闭环。"""

import asyncio
import json
from datetime import timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

import opsswarm.investigation.human_prepare.job_store as job_module
import opsswarm.investigation.human_prepare.store as store_module
from opsswarm.control_plane.app import create_app
from opsswarm.identity.settings import OperatorAuthSettings
from opsswarm.investigation.budget.queries import CallBudgetQueries
from opsswarm.investigation.human_prepare.builder import build_prepared_input
from opsswarm.investigation.human_prepare.contracts import PreparationError
from opsswarm.investigation.human_prepare.job_store import HumanPreparationJobStore
from opsswarm.investigation.human_prepare.store import HumanPreparationStore
from opsswarm.investigation.human_prepare.tables import (
    HumanPreparationArtifactRow,
    HumanPreparationCommandRow,
    HumanPreparationJobRow,
)
from opsswarm.investigation.human_prepare.worker import HumanPreparationWorker
from opsswarm.investigation.persistence.tables import WorkItemRow
from tests.api.test_human_wait_api import command, post
from tests.api.test_human_wait_api import wait_context as wait_context
from tests.identity_helpers import auth_header
from tests.task_facts_helpers import BASE_TIME

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def body(c, *, version=2, key="prepare-command-001", **changes):
    value = command(c, action="cancel", version=version, key=key)
    return {**value, "action": "prepare", **changes}


async def prepare(c, payload=None, *, header=None, run_id=None):
    c.app.state.human_preparation_store = HumanPreparationStore(c.db, clock=c.clock)
    url = (
        f"{c.base}/preparations"
        if run_id is None
        else f"/api/v1/investigations/{run_id}/human-wait/preparations"
    )
    return await c.client.post(
        url, json=body(c) if payload is None else payload, headers=header or auth_header("engineer")
    )


async def get(c, job_id):
    return await c.client.get(f"{c.base}/preparations/{job_id}", headers=auth_header("viewer"))


def worker(c, **kwargs):
    return HumanPreparationWorker(
        HumanPreparationJobStore(c.db, clock=c.clock), worker_id="prepare-worker", **kwargs
    )


async def count(c, table):
    async with c.db() as session:
        return await session.scalar(select(func.count()).select_from(table))


async def test_preparation_worker_preserves_complete_materials_and_original_budget(wait_context):
    c = wait_context
    first = await post(
        c,
        command(
            c,
            items=[
                {"kind": "constraint", "text": "原始约束：禁止执行回滚，也不允许丢掉反证。"},
                {
                    "kind": "statement",
                    "text": "工程师声明：版本仍需核实。",
                    "observed_at": BASE_TIME.isoformat(),
                },
                {"kind": "question", "text": "是否存在竞争性解释？"},
            ],
        ),
    )
    assert first.status_code == 201, first.text
    before_budget = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    before_work = await c.store.get_work_item(c.started.work_item.id)
    before_attempts = await c.store.list_attempts(c.started.run.id)
    created = await prepare(c)
    assert created.status_code == 201, created.text
    job_id = created.json()["job"]["id"]
    assert created.json()["job"]["status"] == "queued"
    assert (await prepare(c)).status_code == 200
    alternate = {key: value for key, value in body(c).items() if key != "action"}
    alternate["reason"] = f"  {alternate['reason']}  "
    assert (
        await c.client.post(
            f"{c.base}/preparations",
            content=json.dumps(alternate, indent=2),
            headers={**auth_header("engineer"), "Content-Type": "application/json"},
        )
    ).status_code == 200
    assert await worker(c).run_once()
    assert not await worker(c).run_once()
    view = (await get(c, job_id)).json()
    assert view["status"] == "succeeded" and view["availability"] == "ready", view
    assert [item["status"] for item in view["attempts"]] == ["succeeded"]
    artifact = view["artifact"]
    assert artifact["diagnostic"]["status"] == "ready"
    packet = artifact["prepared_input"]
    payload = json.loads(packet["model_input"]["messages"][1]["content"])
    assert payload["waiting_version"] == 2
    assert payload["human_inputs"][0] == first.json()["event"]
    async with c.db() as session:
        saved = await session.get(HumanPreparationJobRow, UUID(job_id))
        prior_payload = json.loads(saved.source["prior_input"]["messages"][1]["content"])
    assert payload["original_investigation"] == prior_payload
    assert payload["original_review"] == saved.source["review"]
    assert payload["terminal_route"] == saved.source["route"]
    assert [item["kind"] for item in packet["manifest"]["materials"]] == [
        "original_review_input",
        "prior_review",
        "terminal_route",
        "human_event",
    ]
    assert packet["manifest"]["tool_names"] == packet["manifest"]["trimming"] == []
    assert packet["allocation"]["prompt_version"] == "human-input-review-preparation-v1"
    assert "禁止执行回滚" in packet["model_input"]["messages"][1]["content"]
    assert await CallBudgetQueries(c.db).get_snapshot(c.started.run.id) == before_budget
    assert await c.store.get_work_item(c.started.work_item.id) == before_work
    assert await c.store.list_attempts(c.started.run.id) == before_attempts
    assert (await c.store.get_run(c.started.run.id)).status == "await_human"
    c.clock.current = before_budget.deadline + timedelta(seconds=1)
    expired = (await get(c, job_id)).json()
    assert expired["artifact"] == artifact
    assert (
        expired["availability"] == "blocked"
        and expired["availability_reason"] == "run_deadline_exceeded"
    )
    c.clock.current = BASE_TIME + timedelta(seconds=5)
    assert (await post(c, command(c, version=2, key="supplement-after-prepare"))).status_code == 201
    assert (await get(c, job_id)).json()["availability"] == "stale"
    assert (
        await post(c, command(c, action="cancel", version=3, key="cancel-after-prepare"))
    ).status_code == 201
    historic = (await prepare(c)).json()["job"]
    assert historic["id"] == job_id and historic["artifact"] == artifact
    assert historic["availability_reason"] == "human_wait_cancelled"
    page = await c.client.get(
        f"{c.base}/preparations/commands?limit=1", headers=auth_header("viewer")
    )
    assert page.status_code == 200 and page.json()["next_cursor"] is not None
    assert page.json()["items"][0]["outcome"] == "applied"
    assert "raw_body" not in page.text


async def test_preparation_authentication_strict_body_and_business_refusals(wait_context):
    c = wait_context
    base = f"{c.base}/preparations"
    for path in [f"{base}/commands", f"{base}/{uuid4()}"]:
        assert (await c.client.get(path)).status_code == 401
    assert (await c.client.post(base, json=body(c))).status_code == 401
    import httpx2

    legacy = create_app(operator_auth_settings=OperatorAuthSettings(mode="legacy_local"))
    async with httpx2.AsyncClient(
        transport=httpx2.ASGITransport(app=legacy), base_url="http://test"
    ) as legacy_client:
        assert (await legacy_client.post(base, json=body(c))).status_code == 401
        assert (await legacy_client.get(f"{base}/commands")).status_code == 401
    for role in ["viewer", "approver", "executor", "alert_source"]:
        assert (await prepare(c, header=auth_header(role))).status_code == 403
    for extra in [
        {"actor_id": "forged"},
        {"authorization_id": str(uuid4())},
        {"expected_wait_version": True},
        {"items": []},
        {"action": "resume"},
    ]:
        assert (await prepare(c, body(c, **extra))).status_code == 422
    response = await c.client.post(
        base,
        content=json.dumps(body(c)) + " " * 65536,
        headers={**auth_header("engineer"), "Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert (await prepare(c, body(c, version=1))).json()[
        "error_code"
    ] == "human_preparation_input_required"
    assert (await prepare(c, run_id=uuid4())).status_code == 404
    assert (await post(c, command(c))).status_code == 201
    for change in [
        {"handoff_id": str(uuid4())},
        {"source_sha256": "c" * 64},
        {"expected_wait_version": 3},
    ]:
        assert (await prepare(c, body(c, **change))).status_code == 409
    assert (await prepare(c)).status_code == 201
    assert (await prepare(c, body(c, reason="不同命令内容"))).json()[
        "error_code"
    ] == "human_preparation_idempotency_conflict"
    assert (await prepare(c, body(c, key="another-preparation-key"))).json()[
        "error_code"
    ] == "human_preparation_already_exists"
    assert (await get(c, uuid4())).status_code == 404
    assert await count(c, HumanPreparationJobRow) == 1
    async with c.db() as session:
        failures = await session.scalar(
            select(func.count())
            .select_from(HumanPreparationCommandRow)
            .where(HumanPreparationCommandRow.outcome == "error")
        )
    assert failures == 7


@pytest.mark.parametrize("reason", ["deadline", "window"])
async def test_preparation_blocked_input_is_preserved_without_reserving_budget(
    wait_context, reason
):
    c = wait_context
    if reason == "window":
        items = [{"kind": "constraint", "text": "不可裁剪的人工约束" * 90} for _ in range(5)]
        for index in range(6):
            response = await post(
                c, command(c, version=index + 1, key=f"large-input-{index:04}", items=items)
            )
            assert response.status_code == 201, response.text
        response = await prepare(c, body(c, version=7))
    else:
        assert (await post(c, command(c))).status_code == 201
        original_budget = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
        c.clock.current = original_budget.deadline + timedelta(seconds=1)
        response = await prepare(c)
    assert response.status_code == 201, response.text
    before = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    assert await worker(c).run_once()
    artifact = (await get(c, response.json()["job"]["id"])).json()["artifact"]
    assert artifact["diagnostic"]["status"] == "blocked"
    expected = "run_deadline_exceeded" if reason == "deadline" else "model_input_budget_exceeded"
    assert artifact["diagnostic"]["reason_code"] == expected, artifact["diagnostic"]
    if reason == "window":
        packet = artifact["prepared_input"]
        assert packet["allocation"] is None and packet["input_error_code"] == expected
        assert len(json.loads(packet["model_input"]["messages"][1]["content"])["human_inputs"]) == 6
        assert packet["manifest"]["trimming"] == []
    assert await CallBudgetQueries(c.db).get_snapshot(c.started.run.id) == before


async def test_preparation_retries_infrastructure_failure_with_new_attempt(wait_context):
    c = wait_context
    await post(c, command(c))
    created = await prepare(c)
    job_id = created.json()["job"]["id"]
    invoked = 0

    def flaky(source):
        nonlocal invoked
        invoked += 1
        if invoked == 1:
            raise RuntimeError("可恢复本地组装中断")
        return build_prepared_input(source)

    w = worker(c, builder=flaky)
    assert await w.run_once()
    first = (await get(c, job_id)).json()
    assert first["status"] == "queued" and first["attempts"][0]["status"] == "failed"
    c.clock.current += timedelta(seconds=3)
    assert await w.run_once()
    done = (await get(c, job_id)).json()
    assert done["status"] == "succeeded"
    assert [item["number"] for item in done["attempts"]] == [1, 2]
    assert len({item["id"] for item in done["attempts"]}) == 2


async def test_preparation_expired_claim_is_recovered_and_late_result_rejected(wait_context):
    c = wait_context
    await post(c, command(c))
    created = await prepare(c)
    jobs = HumanPreparationJobStore(c.db, clock=c.clock)
    old = await jobs.claim_next(worker_id="old", lease_duration=timedelta(seconds=1))
    source = await jobs.get_source(old)
    c.clock.current += timedelta(seconds=2)
    fresh = await jobs.claim_next(worker_id="fresh")
    assert fresh.job_id == old.job_id and fresh.attempt_id != old.attempt_id
    with pytest.raises(PreparationError, match="租约无效"):
        await jobs.complete(old, build_prepared_input(source))
    await jobs.complete(fresh, build_prepared_input(await jobs.get_source(fresh)))
    done = (await get(c, created.json()["job"]["id"])).json()
    assert [item["status"] for item in done["attempts"]] == ["lease_expired", "succeeded"]
    assert await count(c, HumanPreparationArtifactRow) == 1


@pytest.mark.parametrize("change", ["supplement", "cancel"])
async def test_preparation_cannot_publish_after_wait_changes(wait_context, change):
    c = wait_context
    await post(c, command(c))
    created = await prepare(c)
    jobs = HumanPreparationJobStore(c.db, clock=c.clock)
    claim = await jobs.claim_next(worker_id="interrupted")
    ready = build_prepared_input(await jobs.get_source(claim))
    assert (
        await post(c, command(c, action=change, version=2, key="input-changed-mid-job"))
    ).status_code == 201
    with pytest.raises(PreparationError, match="改变待准备输入"):
        await jobs.complete(claim, ready)
    await jobs.fail(claim, error_code="human_preparation_wait_changed", retryable=False)
    view = (await get(c, created.json()["job"]["id"])).json()
    assert view["status"] == "failed" and view["availability"] == "stale"
    assert await count(c, HumanPreparationArtifactRow) == 0


async def test_preparation_concurrent_creation_claims_and_fact_guards(wait_context):
    c = wait_context
    await post(c, command(c))
    results = await asyncio.gather(prepare(c), prepare(c, body(c, key="competing-prepare-key")))
    assert sorted(result.status_code for result in results) == [201, 409], [r.text for r in results]
    job_id = next(result.json()["job"]["id"] for result in results if result.status_code == 201)
    jobs = HumanPreparationJobStore(c.db, clock=c.clock)
    claims = await asyncio.gather(
        jobs.claim_next(worker_id="one"), jobs.claim_next(worker_id="two")
    )
    assert sum(item is not None for item in claims) == 1
    claim = next(item for item in claims if item is not None)
    await jobs.complete(claim, build_prepared_input(await jobs.get_source(claim)))
    for table in [
        "investigation_human_preparation_jobs",
        "investigation_human_preparation_attempts",
        "investigation_human_preparation_artifacts",
        "investigation_human_preparation_commands",
    ]:
        with pytest.raises(DBAPIError):
            async with c.db() as session, session.begin():
                await session.execute(text(f"DELETE FROM {table}"))
    assert (await get(c, job_id)).status_code == 200
    async with c.db() as session, session.begin():
        await session.execute(
            text("UPDATE operator_authorizations SET body_sha256=:hash WHERE id=:id"),
            {
                "id": next(
                    r.json()["job"]["authorization_id"] for r in results if r.status_code == 201
                ),
                "hash": "d" * 64,
            },
        )
    assert (await get(c, job_id)).status_code == 409


async def test_preparation_auth_expiry_after_parent_lock_rolls_back_creation(
    wait_context, monkeypatch
):
    c = wait_context
    await post(c, command(c))
    entered = asyncio.Event()
    original = store_module.lock_wait_run

    async def observed(*args, **kwargs):
        entered.set()
        return await original(*args, **kwargs)

    monkeypatch.setattr(store_module, "lock_wait_run", observed)
    async with c.db() as blocker, blocker.begin():
        await blocker.scalar(
            select(WorkItemRow).where(WorkItemRow.id == c.started.work_item.id).with_for_update()
        )
        request = asyncio.create_task(prepare(c))
        await asyncio.wait_for(entered.wait(), 10)
        c.clock.current = BASE_TIME + timedelta(days=1)
    assert (await request).status_code == 401
    assert await count(c, HumanPreparationJobRow) == await count(c, HumanPreparationCommandRow) == 0


async def test_preparation_expiry_after_artifact_flush_rolls_back_success(
    wait_context, monkeypatch
):
    c = wait_context
    await post(c, command(c))
    created = await prepare(c)
    jobs = HumanPreparationJobStore(c.db, clock=c.clock)
    claim = await jobs.claim_next(worker_id="expiring", lease_duration=timedelta(seconds=3))
    prepared = build_prepared_input(await jobs.get_source(claim))
    original = job_module._finish_job

    def expire(*args, **kwargs):
        original(*args, **kwargs)
        c.clock.current += timedelta(seconds=4)

    monkeypatch.setattr(job_module, "_finish_job", expire)
    with pytest.raises(PreparationError, match="提交阶段租约已过期"):
        await jobs.complete(claim, prepared)
    assert await count(c, HumanPreparationArtifactRow) == 0
    assert (await get(c, created.json()["job"]["id"])).json()["status"] == "running"


async def test_preparation_attempt_limit_stops_repeated_infrastructure_failure(wait_context):
    c = wait_context
    await post(c, command(c))
    created = await prepare(c)

    def always_fail(source):
        raise RuntimeError("可复现基础设施失败")

    w = worker(c, builder=always_fail)
    for _ in range(3):
        assert await w.run_once()
        c.clock.current += timedelta(seconds=3)
    assert not await w.run_once()
    view = (await get(c, created.json()["job"]["id"])).json()
    assert view["status"] == "failed" and len(view["attempts"]) == 3
    assert all(attempt["status"] == "failed" for attempt in view["attempts"])
    assert await count(c, HumanPreparationArtifactRow) == 0


async def test_preparation_auth_expiry_after_writes_rolls_back_job_and_audit(
    wait_context, monkeypatch
):
    c = wait_context
    await post(c, command(c))
    original = store_module.read_job

    async def expired_view(*args, **kwargs):
        result = await original(*args, **kwargs)
        c.clock.current = BASE_TIME + timedelta(days=1)
        return result

    monkeypatch.setattr(store_module, "read_job", expired_view)
    response = await prepare(c)
    assert response.status_code == 401, response.text
    assert await count(c, HumanPreparationJobRow) == await count(c, HumanPreparationCommandRow) == 0


@pytest.mark.parametrize("wait_context", ["three_round"], indirect=True)
async def test_preparation_uses_latest_third_round_source_without_new_model_calls(wait_context):
    c = wait_context
    before = await CallBudgetQueries(c.db).get_snapshot(c.started.run.id)
    assert before.reserved_model_calls == 13
    await post(c, command(c))
    async with asyncio.timeout(240):
        created = await prepare(c)
        assert created.status_code == 201, created.text
        assert await worker(c).run_once()
        response = await get(c, created.json()["job"]["id"])
        assert response.status_code == 200, response.text
    view = response.json()
    assert view["status"] == "succeeded"
    payload = json.loads(
        view["artifact"]["prepared_input"]["model_input"]["messages"][1]["content"]
    )
    assert payload["terminal_route"]["decision"]["round_number"] == 3
    assert payload["original_review"]["round_id"] == payload["terminal_route"]["round_id"]
    assert await CallBudgetQueries(c.db).get_snapshot(c.started.run.id) == before
